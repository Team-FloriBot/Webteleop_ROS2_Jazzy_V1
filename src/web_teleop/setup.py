from setuptools import setup, find_packages
from glob import glob
import os

package_name = "web_teleop"

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/web_teleop"]),
        (f"share/{package_name}", ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.py")),
        (os.path.join("share", package_name, "static"),
         glob("web_teleop/static/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    description="Web Teleop publishing Twist to /cmd_vel via WebSocket",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "web_teleop_server = web_teleop.web_teleop_server:main",
        ],
    },
)
