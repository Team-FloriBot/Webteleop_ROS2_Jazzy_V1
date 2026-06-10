from glob import glob
import os

from setuptools import find_packages, setup


package_name = "jury_dashboard"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        (
            os.path.join("share", package_name),
            ["package.xml"],
        ),
        (
            os.path.join("share", package_name, "launch"),
            glob("launch/*.launch.py"),
        ),
        (
            os.path.join("share", package_name, "static"),
            glob(
                os.path.join(
                    package_name,
                    "static",
                    "*",
                )
            ),
        ),
    ],
    install_requires=[
        "setuptools",
    ],
    zip_safe=True,
    maintainer="Timo Zimmermann",
    maintainer_email="tzimmerman@stud.hs-heilbronn.de",
    description=(
        "Read-only web dashboard for FRE 2026 "
        "Task 2 and Task 3 jury information."
    ),
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            (
                "jury_dashboard_server = "
                "jury_dashboard.jury_dashboard_server:main"
            ),
        ],
    },
)
