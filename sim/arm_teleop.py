#!/usr/bin/env python3

import sys
import termios
import tty
import rclpy
from rclpy.node import Node
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from geometry_msgs.msg import Twist
from builtin_interfaces.msg import Duration

msg = """
Universal Vector Teleop
---------------------------
Control the base, torso, arms, and head from one terminal!

Base & Torso:
  w / s : Forward / Backward
  a / d : Turn Left / Right
  q / e : Torso Up / Down
  x     : Stop Base & Torso

Left Arm:
  r / f : shoulder_link_1 (up/down)
  t / g : left_shoulder (up/down)
  y / h : left_bicep (up/down)
  u / j : left_forearm (up/down)

Right Arm:
  i / k : right_shoulder_1 (up/down)
  o / l : right_shoulder_2 (up/down)
  p / ; : right_bicep (up/down)
  [ / ' : right_forearm (up/down)
  7 / 8 : right_wrist_1 (up/down)
  9 / 0 : right_wrist_2 (up/down)

SPACE to reset all arm/head joints to 0.
CTRL-C to quit.
"""

# Base & Torso bindings: key -> (linear.x, linear.z, angular.z)
base_bindings = {
    'w': ( 1.0,  0.0,  0.0),
    's': (-1.0,  0.0,  0.0),
    'a': ( 0.0,  0.0,  1.0),
    'd': ( 0.0,  0.0, -1.0),
    'q': ( 0.0,  1.0,  0.0),
    'e': ( 0.0, -1.0,  0.0),
}

# Arm bindings: key -> (joint_index, direction)
arm_bindings = {
    'r': (0, 1), 'f': (0, -1),
    't': (1, 1), 'g': (1, -1),
    'y': (2, 1), 'h': (2, -1),
    'u': (3, 1), 'j': (3, -1),
    
    'i': (4, 1), 'k': (4, -1),
    'o': (5, 1), 'l': (5, -1),
    'p': (6, 1), ';': (6, -1),
    '[': (7, 1), "'": (7, -1),
    
    '7': (8, 1), '8': (8, -1),
    '9': (9, 1), '0': (9, -1),
}

class UniversalTeleop(Node):
    def __init__(self):
        super().__init__('universal_teleop')
        self.arm_pub_ = self.create_publisher(JointTrajectory, '/joint_trajectory_controller/joint_trajectory', 10)
        self.base_pub_ = self.create_publisher(Twist, '/cmd_vel', 10)
        
        self.joints = [
            "shoulder_link_1", "left_shoulder", "left_bicep", "left_forearm",
            "right_shoulder_1", "right_shoulder_2", "right_bicep", "right_forearm",
            "right_wrist_1", "right_wrist_2"
        ]
        
        self.positions = [0.0] * len(self.joints)
        self.step_size = 0.1
        
        # Base state
        self.lin_x = 0.0
        self.lin_z = 0.0
        self.ang_z = 0.0
        self.speed = 0.5
        self.turn = 1.0

    def publish_trajectory(self):
        traj_msg = JointTrajectory()
        traj_msg.joint_names = self.joints
        
        point = JointTrajectoryPoint()
        point.positions = self.positions
        point.time_from_start = Duration(sec=0, nanosec=100000000) # 0.1s
        
        traj_msg.points.append(point)
        self.arm_pub_.publish(traj_msg)
        
    def publish_twist(self):
        twist_msg = Twist()
        twist_msg.linear.x = self.lin_x * self.speed
        twist_msg.linear.y = 0.0
        twist_msg.linear.z = self.lin_z * self.speed
        twist_msg.angular.x = 0.0
        twist_msg.angular.y = 0.0
        twist_msg.angular.z = self.ang_z * self.turn
        self.base_pub_.publish(twist_msg)

def get_key():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

def main(args=None):
    rclpy.init(args=args)
    node = UniversalTeleop()

    print(msg)
    
    try:
        while True:
            key = get_key()
            if key == '\x03': # CTRL-C
                break
            
            if key in base_bindings:
                dx, dz, dth = base_bindings[key]
                node.lin_x = dx
                node.lin_z = dz
                node.ang_z = dth
                print(f"Base: v={node.lin_x}, w={node.ang_z}, torso={node.lin_z}\r")
                node.publish_twist()
                
            elif key == 'x':
                node.lin_x = 0.0
                node.lin_z = 0.0
                node.ang_z = 0.0
                print("Base stopped\r")
                node.publish_twist()
                
            elif key in arm_bindings:
                idx, dir = arm_bindings[key]
                node.positions[idx] += dir * node.step_size
                node.positions[idx] = max(-3.14, min(3.14, node.positions[idx]))
                print(f"Moved {node.joints[idx]} to {node.positions[idx]:.2f}\r")
                node.publish_trajectory()
                
            elif key == ' ':
                node.positions = [0.0] * len(node.joints)
                print("Reset all arm/head joints to 0.0\r")
                node.publish_trajectory()

    except Exception as e:
        print(e)

    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
