from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package="web_teleop",
            executable="web_teleop_server",
            name="web_teleop_server",
            output="screen",
            parameters=[{
                "cmd_vel_topic": "/cmd_vel",
                "max_linear": 0.25,
                "max_angular": 0.9,
                "timeout_s": 0.3
            }]
        ),
    ])
