from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    cmd_vel_topic = LaunchConfiguration("cmd_vel_topic")
    max_linear = LaunchConfiguration("max_linear")
    max_angular = LaunchConfiguration("max_angular")
    timeout_s = LaunchConfiguration("timeout_s")

    return LaunchDescription([
        DeclareLaunchArgument(
            "cmd_vel_topic",
            default_value="/cmd_vel",
            description="Topic for geometry_msgs/msg/Twist commands",
        ),
        DeclareLaunchArgument(
            "max_linear",
            default_value="3.0",
            description="Backend safety clamp for linear.x in m/s. Frontend slider must not exceed this value.",
        ),
        DeclareLaunchArgument(
            "max_angular",
            default_value="6.0",
            description="Backend safety clamp for angular.z in rad/s.",
        ),
        DeclareLaunchArgument(
            "timeout_s",
            default_value="0.3",
            description="Deadman timeout in seconds",
        ),
        Node(
            package="web_teleop",
            executable="web_teleop_server",
            name="web_teleop_server",
            output="screen",
            parameters=[{
                "cmd_vel_topic": cmd_vel_topic,
                "max_linear": max_linear,
                "max_angular": max_angular,
                "timeout_s": timeout_s,
            }],
        ),
    ])
