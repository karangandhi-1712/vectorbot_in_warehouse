"""
Vector Gazebo simulation launcher (ROS 2 Jazzy + Gazebo Harmonic).

Topic flow:
  Standalone:
      teleop publishes /cmd_vel (Twist)
        ─→ twist_to_stamped (C++ node) ─→ /base_controller/cmd_vel (TwistStamped)

  With OS:
      OS pipeline publishes /cmd_vel_final (Twist)
        ─→ twist_to_stamped (C++ node) ─→ /base_controller/cmd_vel (TwistStamped)
"""
import os
import yaml

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    TimerAction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def resolve_active_joints():
    """The 14 arm + neck joints the JTC controls in simulation."""
    return [
        "left_shoulder_holder", "left_shoulder", "left_bicep", "left_forearm",
        "right_shoulder_holder", "right_shoulder", "right_bicep", "right_forearn",
        "neck", "head",
    ]


def write_jtc_override(joints, out_path):
    payload = {"joint_trajectory_controller": {"ros__parameters": {"joints": joints}}}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(payload, f)


def find_os_launch_file(os_ws: str):
    home = os.path.expanduser("~")
    candidates = []
    if os_ws:
        ws = os.path.expanduser(os_ws)
        candidates.append(os.path.join(ws, "install", "vector_hardware_controllers",
                                       "share", "vector_hardware_controllers",
                                       "launch", "main.launch.py"))
    try:
        pkg = get_package_share_directory("vector_hardware_controllers")
        candidates.append(os.path.join(pkg, "launch", "main.launch.py"))
    except Exception:
        pass
    for guess in ["vector_os", "ros2_ws", "vector_ws"]:
        candidates.append(os.path.join(home, guess, "install", "vector_hardware_controllers",
                                       "share", "vector_hardware_controllers",
                                       "launch", "main.launch.py"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def make_bridge(input_topic: str, name: str):
    """Spawn the C++ twist_to_stamped bridge with custom topic remap."""
    return Node(
        package="vector_gazebo_sim",
        executable="twist_to_stamped",
        name=name,
        parameters=[{
            "use_sim_time":  True,
            "input_topic":   input_topic,
            "output_topic":  "/base_controller/cmd_vel",
            "frame_id":      "base_link",
        }],
        output="screen",
    )


def launch_setup(context, *args, **kwargs):
    pkg_share = get_package_share_directory("vector_description")
    gazebo_pkg_share = get_package_share_directory("vector_gazebo_sim")

    headless   = LaunchConfiguration("headless").perform(context).lower() == "true"
    launch_os  = LaunchConfiguration("launch_os").perform(context).lower() == "true"
    world_file = LaunchConfiguration("world").perform(context)
    os_ws      = LaunchConfiguration("os_ws").perform(context)

    # JTC joint override file (read by URDF gz_ros2_control plugin)
    jtc_override_path = os.path.join(gazebo_pkg_share, "config", "jtc_joints_override.yaml")
    write_jtc_override(resolve_active_joints(), jtc_override_path)

    # Path to ros2_controllers config — passed to xacro to override the hardcoded path in URDF
    # The URDF's gz_ros2_control plugin needs this path to load controller configurations
    ros2_controllers_config = os.path.join(gazebo_pkg_share, "config", "gz_ros2_control_config.yaml")

    # Use vector.xacro which has all the proper mesh files for the humanoid robot
    # industrial_robot.urdf.xacro only includes primitive geometry (boxes, cylinders) with no real meshes
    urdf_xacro = os.path.join(pkg_share, "urdf", "vector.xacro")
    # Pass the config file path as a xacro argument so the URDF can use it dynamically
    # Construct as a single shell command string for reliable argument passing
    robot_description_content = ParameterValue(
        Command(f"xacro {urdf_xacro} ros2_controllers_config:={ros2_controllers_config}"),
        value_type=str,
    )
    robot_description_params = {
        "robot_description": robot_description_content,
        "use_sim_time": True,
    }

    # ── Gazebo Harmonic ──────────────────────────────────────────────
    world_path = os.path.join(gazebo_pkg_share, "worlds", world_file)
    gz_args = f"-r {world_path}"
    if headless:
        gz_args = f"-r -s --headless-rendering {world_path}"
    gz_sim = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare("ros_gz_sim"),
                                  "launch", "gz_sim.launch.py"])
        ),
        launch_arguments={"gz_args": gz_args}.items(),
    )

    rsp = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        parameters=[robot_description_params],
        output="screen",
    )

    spawn_robot = Node(
        package="ros_gz_sim",
        executable="create",
        arguments=["-name", "vector_robot", "-topic", "/robot_description",
                   "-z", "0.05"],
        output="screen",
    )

    bridge_config = os.path.join(gazebo_pkg_share, "config", "bridge.yaml")
    bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        parameters=[{"config_file": bridge_config, "use_sim_time": True}],
        output="screen",
    )

    # ── Spawners (delayed for plugin to come up) ──────────────────────
    def make_spawner(name, extra_args=None):
        args = [name, "--controller-manager-timeout", "60"]
        if extra_args:
            args += extra_args
        return Node(
            package="controller_manager",
            executable="spawner",
            arguments=args,
            parameters=[{"use_sim_time": True}],
            output="screen",
        )

    delayed_spawners = TimerAction(
        period=5.0,
        actions=[
            make_spawner("joint_state_broadcaster"),
            make_spawner("base_controller"),
            make_spawner("joint_trajectory_controller"),
            make_spawner("torso_effort_controller"),
        ],
    )

    # ── Torso position PID (software loop, outputs effort) ────────────
    # Architecture (see src/torso_position_pid.cpp for full explanation):
    #   /torso_position_controller/commands  (Float64MultiArray, OS publishes)
    #     → torso_position_pid (this node)
    #     → /torso_effort_controller/commands
    #     → effort_controllers/JointGroupEffortController
    #     → torso_z_joint effort interface in Gazebo
    #
    # Gains derived from physics (m=13kg, ωn=4 rad/s, ζ=1.0):
    #   kp = m·ωn² = 208      kd = 2·ζ·m·ωn = 104
    # gravity_feedforward_n = m·g = 127 N (cancels weight)
    # ki = 0 (FF holds steady state, I term causes windup on vertical joints)
    #
    # Live tuning: ros2 param set /torso_position_pid <param> <value>
    torso_pid = Node(
        package="vector_gazebo_sim",
        executable="torso_position_pid",
        name="torso_position_pid",
        parameters=[{
            "use_sim_time":            True,
            "joint_name":              "slider_z_axis",
            "kp":                      208.0,
            "ki":                      0.0,
            "kd":                      104.0,
            "gravity_feedforward_n":   127.0,
            "max_effort":              2000.0,
            "min_effort":             -2000.0,
            "min_position":            0.0,
            "max_position":            0.39,    # new z_travel from real measurements
            "integral_clamp":          100.0,
            "setpoint_slew_m_per_s":   0.15,
            "deadband_m":              0.001,
        }],
        output="screen",
    )

    # ── Torso arm-collision safety guard ─────────────────────────────
    # Intercepts torso commands BEFORE they reach the PID. If arms are
    # in an unsafe pose (hanging down), refuses to lower the torso below
    # safe_min_torso_z. Republishes safe commands on .../commands_safe
    # which torso_position_pid actually subscribes to.
    #
    # Live tunable: ros2 param set /torso_safety_guard <param> <value>
    torso_safety = Node(
        package="vector_gazebo_sim",
        executable="torso_safety_guard",
        name="torso_safety_guard",
        parameters=[{
            "use_sim_time":                   True,
            "safe_min_torso_z":               0.30,   # don't go below 0.30 if arms unsafe
            "shoulder_safe_threshold_rad":    0.6,    # arm is safe if |shoulder_1| > 0.6 rad
            "elbow_safe_threshold_rad":       0.9,    # ... or if |elbow_2| > 0.9 rad
            "max_torso_z":                    0.39,
            "verbose":                        True,
        }],
        output="screen",
    )

    # ── cmd_vel routing via C++ bridge ────────────────────────────────
    # Static TF for the two fixed camera joints.
    # robot_state_publisher sends these on /tf_static (latched, once).
    # Over zenoh that one-shot broadcast is occasionally missed, causing
    # "Could not transform from camera_optical_frame to base_link" errors
    # in RViz.  static_transform_publisher re-broadcasts at 10 Hz so the
    # transform is always in the TF buffer regardless of transport drops.
    camera_link_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="camera_link_static_tf",
        arguments=["0.10", "0", "0.08", "0", "0", "0",
                   "head_link", "camera_link"],
        parameters=[{"use_sim_time": True}],
        output="log",
    )
    camera_optical_tf = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="camera_optical_static_tf",
        arguments=["0", "0", "0", "-1.5708", "0", "-1.5708",
                   "camera_link", "camera_optical_frame"],
        parameters=[{"use_sim_time": True}],
        output="log",
    )

    nodes = [
        gz_sim, rsp, bridge, spawn_robot, delayed_spawners,
        torso_safety,
        torso_pid,
        camera_link_tf,
        camera_optical_tf,
        make_bridge("/cmd_vel_final", "twist_bridge_final"),
    ]

    # Standalone teleop also publishes plain Twist on /cmd_vel — bridge it too
    if not launch_os:
        nodes.append(make_bridge("/cmd_vel", "twist_bridge_direct"))
        print("[vector_gazebo_sim] Standalone mode: /cmd_vel and /cmd_vel_final both bridged.")
    else:
        print("[vector_gazebo_sim] OS mode: only /cmd_vel_final is bridged (OS handles /cmd_vel).")

    # ── Optional embedded OS launch ──────────────────────────────────
    if launch_os:
        launch_path = find_os_launch_file(os_ws)
        if launch_path:
            print(f"[vector_gazebo_sim] Embedding OS launch: {launch_path}")
            os_launch = IncludeLaunchDescription(
                PythonLaunchDescriptionSource(launch_path),
                launch_arguments={"use_sim": "true"}.items(),
            )
            nodes.append(TimerAction(period=8.0, actions=[os_launch]))
        else:
            print(
                "\n[vector_gazebo_sim] ERROR: cannot locate vector_hardware_controllers.\n"
                "  Run with: launch_os:=true os_ws:=/home/ishaan/vector_os\n"
                "  Or source ~/vector_os/install/setup.bash before launching.\n"
            )

    return nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("world", default_value="empty.sdf"),
        DeclareLaunchArgument("headless", default_value="false"),
        DeclareLaunchArgument("launch_os", default_value="false"),
        DeclareLaunchArgument("os_ws", default_value=""),
        OpaqueFunction(function=launch_setup),
    ])