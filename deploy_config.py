"""Where the robot is and how to log in to it.

Kept apart from conversation.py on purpose: the deploy tools
(tools/deploy_robot_app.py, tools/deploy_robot_player.py) need these few
constants and nothing else, and conversation.py pulls in google-genai, scipy,
sounddevice and the rest of the speech stack. With the constants here, the hub
can run a deploy with its own Python (only paramiko installed), and a student
laptop never needs reachy_chat's virtual environment.

conversation.py re-exports every name below, so `from conversation import
ROBOT_HOST` keeps working everywhere.

The SSH login is the robot's factory default (it is printed in Pollen's own
documentation). Override it with the ROBOT_USER / ROBOT_PASSWORD environment
variables.
"""

from __future__ import annotations

import os

# The robot's address on the home WiFi. It changes when the robot joins a
# different network; pass --robot-host (or set REACHY_ROBOT_HOST) instead of
# editing this file.
ROBOT_HOST_DEFAULT = "10.100.102.18"
ROBOT_HOST = os.environ.get("REACHY_ROBOT_HOST", ROBOT_HOST_DEFAULT)
ROBOT_USER = os.environ.get("ROBOT_USER", "pollen")
ROBOT_PASSWORD = os.environ.get("ROBOT_PASSWORD", "root")

# The daemon's own virtual environment on the robot; the app runs inside it.
ROBOT_PYTHON = "/venvs/mini_daemon/bin/python"
ROBOT_STREAMING_PLAYER = "/home/pollen/scripts/robot_streaming_player.py"

# Where tools/deploy_robot_app.py puts the app on the robot.
REMOTE_ROOT = "/home/pollen/reachy_chat"
