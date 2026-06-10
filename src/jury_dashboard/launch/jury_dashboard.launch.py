from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    classification_topic = LaunchConfiguration(
        "classification_topic"
    )
    web_host = LaunchConfiguration("web_host")
    web_port = LaunchConfiguration("web_port")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "classification_topic",
                default_value="/classification_result",
                description=(
                    "std_msgs/msg/String topic containing "
                    "classification events."
                ),
            ),
            DeclareLaunchArgument(
                "web_host",
                default_value="0.0.0.0",
                description=(
                    "Network address on which the jury "
                    "dashboard listens."
                ),
            ),
            DeclareLaunchArgument(
                "web_port",
                default_value="8081",
                description="TCP port of the jury dashboard.",
            ),
            Node(
                package="jury_dashboard",
                executable="jury_dashboard_server",
                name="jury_dashboard_server",
                output="screen",
                parameters=[
                    {
                        "classification_topic":
                            classification_topic,
                        "web_host": web_host,
                        "web_port": web_port,
                    }
                ],
            ),
        ]
    )
