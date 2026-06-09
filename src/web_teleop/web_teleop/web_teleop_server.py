"""ROS 2 backed WebTeleop server with cmd_vel source selection."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import rclpy
from ament_index_python.packages import get_package_share_directory
from cmd_vel_selector.srv import SelectSource
from fre2026_tasks_interfaces.srv import SetScanProfile
from maize_navigation_interfaces.srv import StartNavigation
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry, Path as RosPath
from visualization_msgs.msg import Marker
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger
try:
    from slam_toolbox.srv import Reset as SlamToolboxReset
except ImportError:  # pragma: no cover - depends on ROS installation
    SlamToolboxReset = None
import uvicorn


VALID_SOURCES = ("none", "webteleop", "tasks")
TASK_SOURCE = "tasks"
SOURCE_LABELS = {
    "none": "Stop",
    "webteleop": "Webteleop",
    "tasks": "Tasks",
    "task1": "Task 1",
    "task2": "Task 2",
    "task3": "Task 3",
    "task4": "Task 4",
}


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        payload = json.dumps(message)

        async with self._lock:
            connections = tuple(self._connections)

        for websocket in connections:
            try:
                await websocket.send_text(payload)
            except Exception:
                dead.append(websocket)

        if dead:
            async with self._lock:
                for websocket in dead:
                    self._connections.discard(websocket)


class WebTeleopNode(Node):
    def __init__(
        self,
        event_loop: asyncio.AbstractEventLoop,
        manager: ConnectionManager,
    ) -> None:
        super().__init__("web_teleop_server")

        self._loop = event_loop
        self._manager = manager
        self._active_source = "unknown"
        self._command_lock = threading.Lock()
        self._latest_linear_x = 0.0
        self._latest_angular_z = 0.0
        self._last_cmd_time = time.monotonic()
        self._moving_command_active = False
        self._runtime_lock = threading.Lock()
        self._running_task: str | None = None
        self._latest_task4_plan: dict[str, Any] | None = None
        self._latest_task4_polygon: dict[str, Any] | None = None
        self._latest_task4_robot_pose: dict[str, Any] | None = None
        self._available_models: list[str] = []
        self._task_navigation_config: dict[str, dict[str, str]] = {
            "task1": {"pattern": "", "carefulness": "high", "model_path": ""},
            "task2": {
                "pattern": "",
                "carefulness": "high",
                "model_path": "/models/yolo26n_jutestripe_yellowpaper-seg.pt",
            },
            "task3": {
                "pattern": "",
                "carefulness": "high",
                "model_path": "/models/yolo26n_bee_beetle_butterfly-seg.pt",
            },
        }

        self._cmd_vel_topic = self.declare_parameter(
            "cmd_vel_topic",
            "/cmd_vel/webteleop",
        ).value
        self._max_linear = float(
            self.declare_parameter("max_linear", 1.0).value
        )
        self._max_angular = float(
            self.declare_parameter("max_angular", 0.9).value
        )
        self._timeout_s = float(
            self.declare_parameter("timeout_s", 0.75).value
        )
        self._odom_topic = self.declare_parameter(
            "odom_topic",
            "/odom",
        ).value

        self._publisher = self.create_publisher(
            Twist,
            self._cmd_vel_topic,
            10,
        )

        selector_qos = QoSProfile(depth=1)
        selector_qos.reliability = ReliabilityPolicy.RELIABLE
        selector_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

        self._status_subscription = self.create_subscription(
            String,
            "/cmd_vel_selector/active_source",
            self._active_source_callback,
            selector_qos,
        )

        self._select_client = self.create_client(
            SelectSource,
            "/cmd_vel_selector/select_source",
        )
        self._start_navigation_client = self.create_client(
            StartNavigation,
            "/start_navigation",
        )
        self._stop_navigation_client = self.create_client(
            Trigger,
            "/stop_navigation",
        )
        self._pause_navigation_client = self.create_client(
            Trigger,
            "/pause_navigation",
        )
        self._resume_navigation_client = self.create_client(
            Trigger,
            "/resume_navigation",
        )
        self._reset_navigation_client = self.create_client(
            Trigger,
            "/reset_navigation",
        )
        self._front_scan_mux_client = self.create_client(
            SetScanProfile,
            "/set_profile",
        )
        self._slam_reset_client = (
            self.create_client(SlamToolboxReset, "/slam_toolbox/reset")
            if SlamToolboxReset is not None
            else None
        )
        self._ptu_reference_client = self.create_client(
            Trigger,
            "/ptu/reference",
        )
        self._task4_set_polygon_client = self.create_client(
            SetParameters,
            "/task4_brain/set_parameters",
        )
        self._task4_plan_client = self.create_client(
            Trigger,
            "/task4/plan_coverage",
        )
        self._task4_start_client = self.create_client(
            Trigger,
            "/task4/start_navigation",
        )
        self._task4_stop_client = self.create_client(
            Trigger,
            "/task4/stop_navigation",
        )
        self._task4_reset_client = self.create_client(
            Trigger,
            "/task4/reset",
        )

        self._task4_plan_subscription = self.create_subscription(
            RosPath,
            "/plan",
            self._task4_plan_callback,
            10,
        )
        self._task4_polygon_subscription = self.create_subscription(
            Marker,
            "/task4/coverage_polygon_marker",
            self._task4_polygon_callback,
            10,
        )
        self._odom_subscription = self.create_subscription(
            Odometry,
            self._odom_topic,
            self._odom_callback,
            10,
        )
        self._available_models_subscription = self.create_subscription(
            String,
            "/detector/available_models",
            self._available_models_callback,
            10,
        )

        self._publish_timer = self.create_timer(
            0.05,
            self._publish_timer_callback,
        )

        self.get_logger().info(
            f"Webteleop publiziert Fahrbefehle mit 20 Hz auf '{self._cmd_vel_topic}'."
        )

    def available_models_payload(self) -> dict[str, Any]:
        return {
            "type": "available_models",
            "models": list(self._available_models),
        }

    def _available_models_callback(self, msg: String) -> None:
        try:
            models = json.loads(msg.data)
            if not isinstance(models, list):
                raise ValueError("available_models payload is not a list")

            normalized: list[str] = []
            seen: set[str] = set()
            for model in models:
                model_path = str(model).strip()
                if model_path and model_path not in seen:
                    normalized.append(model_path)
                    seen.add(model_path)

            self._available_models = normalized
            self._schedule_broadcast(self.available_models_payload())
        except Exception as exc:
            self.get_logger().error(f"Failed to parse available models: {exc}")

    @property
    def active_source(self) -> str:
        return self._active_source

    @property
    def running_task(self) -> str | None:
        with self._runtime_lock:
            return self._running_task

    def _begin_task_start(self, task: str) -> tuple[bool, str]:
        with self._runtime_lock:
            if self._running_task is not None:
                running_label = SOURCE_LABELS.get(self._running_task, self._running_task)
                return False, f"{running_label} läuft bereits. Bitte zuerst stoppen."
            self._running_task = task
            return True, ""

    def _finish_task_start(self, task: str, success: bool) -> None:
        if success:
            return
        with self._runtime_lock:
            if self._running_task == task:
                self._running_task = None

    def _finish_task_stop(self, task: str, success: bool) -> None:
        if not success:
            return
        with self._runtime_lock:
            if self._running_task == task:
                self._running_task = None

    def _runtime_fields(self) -> dict[str, Any]:
        return {"running_task": self.running_task}

    def status_payload(self) -> dict[str, Any]:
        return {
            "type": "selector_status",
            "active_source": self._active_source,
            "active_source_label": SOURCE_LABELS.get(
                self._active_source,
                self._active_source,
            ),
            "running_task": self.running_task,
            "valid_sources": [
                {
                    "id": source,
                    "label": SOURCE_LABELS[source],
                }
                for source in VALID_SOURCES
            ],
        }

    def update_cmd_vel(self, linear_x: float, angular_z: float) -> None:
        linear_x = max(
            -self._max_linear,
            min(self._max_linear, float(linear_x)),
        )
        angular_z = max(
            -self._max_angular,
            min(self._max_angular, float(angular_z)),
        )

        with self._command_lock:
            self._latest_linear_x = linear_x
            self._latest_angular_z = angular_z
            self._last_cmd_time = time.monotonic()
            self._moving_command_active = linear_x != 0.0 or angular_z != 0.0

    def _publish_twist(self, linear_x: float, angular_z: float) -> None:
        message = Twist()
        message.linear.x = linear_x
        message.angular.z = angular_z
        self._publisher.publish(message)

    def stop(self) -> None:
        with self._command_lock:
            self._latest_linear_x = 0.0
            self._latest_angular_z = 0.0
            self._last_cmd_time = time.monotonic()
            self._moving_command_active = False

        self._publish_twist(0.0, 0.0)

    def select_source(self, source: str, websocket: WebSocket) -> None:
        if source not in VALID_SOURCES:
            self._schedule_send(
                websocket,
                {
                    "type": "selection_result",
                    "success": False,
                    "message": "Ungültige Quelle.",
                    "active_source": self._active_source,
                },
            )
            return

        running_task = self.running_task
        if running_task is not None and source not in {TASK_SOURCE, "none"}:
            self._schedule_send(
                websocket,
                {
                    "type": "selection_result",
                    "success": False,
                    "message": f"{SOURCE_LABELS.get(running_task, running_task)} läuft bereits. Bitte zuerst stoppen, bevor die Quelle gewechselt wird.",
                    "active_source": self._active_source,
                    **self._runtime_fields(),
                },
            )
            return

        if not self._select_client.service_is_ready():
            self._select_client.wait_for_service(timeout_sec=0.02)

        if not self._select_client.service_is_ready():
            self._schedule_send(
                websocket,
                {
                    "type": "selection_result",
                    "success": False,
                    "message": "cmd_vel_selector-Service ist nicht erreichbar.",
                    "active_source": self._active_source,
                },
            )
            return

        request = SelectSource.Request()
        request.source = source

        future = self._select_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "selection_result",
                    "success": bool(response.success),
                    "message": response.message,
                    "active_source": response.active_source,
                    "active_source_label": SOURCE_LABELS.get(
                        response.active_source,
                        response.active_source,
                    ),
                    **self._runtime_fields(),
                }
            except Exception as exc:
                payload = {
                    "type": "selection_result",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                    "active_source": self._active_source,
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    @staticmethod
    def _service_ready(client: Any) -> bool:
        if not client.service_is_ready():
            client.wait_for_service(timeout_sec=0.02)
        return bool(client.service_is_ready())

    @staticmethod
    def _normalize_carefulness(value: Any) -> str:
        valid_profiles = {
            "high",
            "medium",
            "low",
            "high_laser",
            "high_mix",
            "high_map",
            "medium_laser",
            "medium_mix",
            "medium_map",
            "low_laser",
            "low_mix",
            "low_map",
        }
        carefulness = str(value or "high").strip().lower()
        return carefulness if carefulness in valid_profiles else "high"

    @staticmethod
    def _validate_pattern(pattern: str) -> tuple[bool, str]:
        import re
        pattern = str(pattern or "").strip()
        if not pattern:
            return False, "Pattern darf nicht leer sein."
        invalid = [token for token in pattern.split() if re.fullmatch(r"([1-9][0-9]*)([LlRr])", token) is None]
        if invalid:
            return False, "Ungueltiges Pattern. Verwende z. B. '1L 2R 3L'."
        return True, pattern

    def configure_task_navigation(
        self,
        task: str,
        pattern: str,
        carefulness: str,
        model_path: str,
        websocket: WebSocket,
    ) -> None:
        if task not in self._task_navigation_config:
            self._schedule_send(websocket, {
                "type": "task_config_result",
                "task": task,
                "success": False,
                "message": "Unbekannte Task-Konfiguration.",
            })
            return

        ok, normalized_pattern = self._validate_pattern(pattern)
        if not ok:
            self._schedule_send(websocket, {
                "type": "task_config_result",
                "task": task,
                "success": False,
                "message": normalized_pattern,
            })
            return

        config = {
            "pattern": normalized_pattern,
            "carefulness": self._normalize_carefulness(carefulness),
            "model_path": "" if task == "task1" else str(model_path or "").strip(),
        }
        self._task_navigation_config[task] = config
        message = f"{SOURCE_LABELS.get(task, task)}: Pattern '{config['pattern']}', carefulness '{config['carefulness']}' uebernommen."
        if config["model_path"]:
            message += f" model_path: {config['model_path']}"
        self._schedule_send(websocket, {
            "type": "task_config_result",
            "task": task,
            "success": True,
            "message": message,
            **config,
        })

    def request_navigation_status(self, websocket: WebSocket) -> None:
        self._schedule_send(websocket, {
            "type": "navigation_status",
            "success": True,
            "message": "Im aktuellen FRE2026_Tasks-Repo ist kein /get_navigation_status-Service implementiert.",
            "mission_state": "nicht verfuegbar",
            "pattern_loaded": bool(self._task_navigation_config["task1"].get("pattern")),
            "active_pattern": self._task_navigation_config["task1"].get("pattern", ""),
            "active_step_index": 0,
            "total_steps": 0,
            "active_step": "",
            "can_set_pattern": True,
            "can_start": True,
            "can_pause": False,
            "can_resume": False,
            "can_abort": True,
            **self._runtime_fields(),
        })

    def trigger_navigation(
        self,
        command: str,
        confirmed: bool,
        websocket: WebSocket,
        pattern: str = "",
        carefulness: str = "high",
    ) -> None:
        confirmation_required = command in {"start", "pause"}
        if confirmation_required and not confirmed:
            self._schedule_send(websocket, {
                "type": "task_result",
                "command": command,
                "success": False,
                "message": "Befehl verworfen: explizite Bestätigung fehlt.",
            })
            return

        if command == "start":
            config = dict(self._task_navigation_config["task1"])
            if pattern:
                ok, normalized_pattern = self._validate_pattern(pattern)
                if not ok:
                    self._schedule_send(websocket, {
                        "type": "task_result",
                        "task": "task1",
                        "command": "start",
                        "success": False,
                        "message": normalized_pattern,
                        **self._runtime_fields(),
                    })
                    return
                config["pattern"] = normalized_pattern
            if carefulness:
                config["carefulness"] = self._normalize_carefulness(carefulness)
            config["model_path"] = ""
            self._task_navigation_config["task1"] = config
            self._start_navigation_task("task1", "task_result", websocket, config)
            return

        clients = {
            "stop": self._stop_navigation_client,
            "pause": self._pause_navigation_client,
            "resume": self._resume_navigation_client,
        }
        client = clients.get(command)
        if client is None:
            self._schedule_send(websocket, {
                "type": "task_result",
                "command": command,
                "success": False,
                "message": "Unbekannter Navigationsbefehl.",
            })
            return

        self._trigger_task_client(
            "task1",
            client,
            command,
            f"Navigation-Service '{command}' ist nicht erreichbar.",
            "task_result",
            websocket,
        )

    def reset_navigation(self, websocket: WebSocket, task: str = "task1") -> None:
        task = str(task or "task1").strip()
        if task not in {"task1", "task2", "task3"}:
            task = "task1"
        result_type = "task_result" if task == "task1" else "task_result_generic"
        if not self._service_ready(self._reset_navigation_client):
            self._schedule_send(
                websocket,
                {
                    "type": result_type,
                    "task": task,
                    "command": "reset",
                    "success": False,
                    "message": "Reset-Service '/reset_navigation' ist nicht erreichbar.",
                },
            )
            return

        future = self._reset_navigation_client.call_async(Trigger.Request())

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                success = bool(response.success)
                if success:
                    self._finish_task_stop(task, True)
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": "reset",
                    "success": success,
                    "message": response.message,
                    **self._runtime_fields(),
                }
            except Exception as exc:
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": "reset",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def reset_slam_map(self, websocket: WebSocket) -> None:
        if self._slam_reset_client is None:
            self._schedule_send(websocket, {
                "type": "development_result",
                "command": "reset_slam_map",
                "success": False,
                "message": "slam_toolbox/srv/Reset ist im Webteleop-Container nicht verfügbar.",
            })
            return
        if not self._service_ready(self._slam_reset_client):
            self._schedule_send(websocket, {
                "type": "development_result",
                "command": "reset_slam_map",
                "success": False,
                "message": "SLAM-Reset-Service '/slam_toolbox/reset' ist nicht erreichbar.",
            })
            return

        request = SlamToolboxReset.Request()
        request.pause_new_measurements = False
        future = self._slam_reset_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                result = getattr(response, "result", 0)
                payload = {
                    "type": "development_result",
                    "command": "reset_slam_map",
                    "success": True,
                    "message": f"SLAM-Map wurde über '/slam_toolbox/reset' zurückgesetzt. result={result}",
                }
            except Exception as exc:
                payload = {
                    "type": "development_result",
                    "command": "reset_slam_map",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }
            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def reference_ptu(self, websocket: WebSocket) -> None:
        if not self._service_ready(self._ptu_reference_client):
            self._schedule_send(
                websocket,
                {
                    "type": "development_result",
                    "command": "reference_ptu",
                    "success": False,
                    "message": (
                        "PTU-Referenz-Service '/ptu/reference' "
                        "ist nicht erreichbar."
                    ),
                },
            )
            return

        future = self._ptu_reference_client.call_async(Trigger.Request())

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "development_result",
                    "command": "reference_ptu",
                    "success": bool(response.success),
                    "message": response.message,
                }
            except Exception as exc:
                payload = {
                    "type": "development_result",
                    "command": "reference_ptu",
                    "success": False,
                    "message": (
                        "Aufruf der PTU-Referenzfahrt fehlgeschlagen: "
                        f"{exc}"
                    ),
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def set_front_scan_profile(self, profile: str, websocket: WebSocket) -> None:
        profile = str(profile or "").strip()
        if profile not in {
            "rs_crop_scan",
            "rs_nonground_scan",
            "sick_front",
            "rs_nonground_scan_torsten",
        }:
            self._schedule_send(websocket, {
                "type": "development_result",
                "command": "set_scan_profile",
                "success": False,
                "message": "Ungültiges Laser-Profil.",
            })
            return
        if not self._service_ready(self._front_scan_mux_client):
            self._schedule_send(websocket, {
                "type": "development_result",
                "command": "set_scan_profile",
                "success": False,
                "message": "Laser-Mux-Service '/set_profile' ist nicht erreichbar.",
            })
            return

        request = SetScanProfile.Request()
        request.profile = profile
        future = self._front_scan_mux_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "development_result",
                    "command": "set_scan_profile",
                    "success": bool(response.success),
                    "message": response.message,
                    "active_profile": response.active_profile,
                }
            except Exception as exc:
                payload = {
                    "type": "development_result",
                    "command": "set_scan_profile",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }
            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def set_task4_polygon(self, polygon_coords: list[Any], websocket: WebSocket) -> None:
        try:
            coords = [float(value) for value in polygon_coords]
            if len(coords) < 6 or len(coords) % 2 != 0:
                raise ValueError("polygon_coords benötigt mindestens drei x/y-Paare.")
        except (TypeError, ValueError) as exc:
            self._schedule_send(
                websocket,
                {
                    "type": "task4_result",
                    "command": "set_polygon",
                    "success": False,
                    "message": f"Ungültige Eckpunkte: {exc}",
                },
            )
            return

        if not self._service_ready(self._task4_set_polygon_client):
            self._schedule_send(
                websocket,
                {
                    "type": "task4_result",
                    "command": "set_polygon",
                    "success": False,
                    "message": "Parameter-Service '/task4_brain/set_parameters' ist nicht erreichbar.",
                },
            )
            return

        parameter_value = ParameterValue(
            type=ParameterType.PARAMETER_DOUBLE_ARRAY,
            double_array_value=coords,
        )
        request = SetParameters.Request()
        request.parameters = [
            Parameter(
                name="polygon_coords",
                value=parameter_value,
            )
        ]
        future = self._task4_set_polygon_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                results = list(response.results)
                success = bool(results) and all(result.successful for result in results)
                reason = "; ".join(result.reason for result in results if result.reason)
                message = (
                    "Task-4-Eckpunkte wurden gesetzt."
                    if success
                    else f"Task-4-Eckpunkte wurden abgelehnt: {reason or 'ohne Begründung'}"
                )
                payload = {
                    "type": "task4_result",
                    "command": "set_polygon",
                    "success": success,
                    "message": message,
                }
            except Exception as exc:
                payload = {
                    "type": "task4_result",
                    "command": "set_polygon",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def trigger_task4_planning(self, websocket: WebSocket) -> None:
        self._trigger_task4_client(
            self._task4_plan_client,
            "plan",
            "Task-4-Planungsservice '/task4/plan_coverage' ist nicht erreichbar.",
            websocket,
        )

    def trigger_generic_task_command(
        self,
        task: str,
        command: str,
        websocket: WebSocket,
        pattern: str = "",
        carefulness: str = "high",
        model_path: str = "",
    ) -> None:
        if task not in {"task2", "task3"}:
            self._schedule_send(websocket, {
                "type": "task_result_generic",
                "task": task,
                "command": command,
                "success": False,
                "message": "Unbekannter Task-Befehl.",
                **self._runtime_fields(),
            })
            return
        if command == "stop":
            self._trigger_task_client(
                task,
                self._stop_navigation_client,
                "stop",
                "StopNavigation-Service '/stop_navigation' ist nicht erreichbar.",
                "task_result_generic",
                websocket,
            )
            return

        if command != "start":
            self._schedule_send(websocket, {
                "type": "task_result_generic",
                "task": task,
                "command": command,
                "success": False,
                "message": "Unbekannter Task-Befehl.",
                **self._runtime_fields(),
            })
            return

        config = dict(self._task_navigation_config[task])
        if pattern:
            ok, normalized_pattern = self._validate_pattern(pattern)
            if not ok:
                self._schedule_send(websocket, {
                    "type": "task_result_generic",
                    "task": task,
                    "command": "start",
                    "success": False,
                    "message": normalized_pattern,
                    **self._runtime_fields(),
                })
                return
            config["pattern"] = normalized_pattern
        if carefulness:
            config["carefulness"] = self._normalize_carefulness(carefulness)
        config["model_path"] = str(model_path or config.get("model_path", "")).strip()
        self._task_navigation_config[task] = config
        self._start_navigation_task(task, "task_result_generic", websocket, config)

    def trigger_task4_command(self, command: str, websocket: WebSocket) -> None:
        clients = {
            "start": self._task4_start_client,
            "stop": self._task4_stop_client,
            "reset": self._task4_reset_client,
        }
        client = clients.get(command)
        if client is None:
            self._schedule_send(
                websocket,
                {
                    "type": "task4_result",
                    "task": "task4",
                    "command": command,
                    "success": False,
                    "message": "Unbekannter Task-4-Befehl.",
                    **self._runtime_fields(),
                },
            )
            return

        if command == "start" and self._active_source != TASK_SOURCE:
            self._schedule_send(
                websocket,
                {
                    "type": "task4_result",
                    "task": "task4",
                    "command": command,
                    "success": False,
                    "message": "Tasks muss in der cmd_vel-Weiche als aktive Quelle ausgewählt sein.",
                    **self._runtime_fields(),
                },
            )
            return

        self._trigger_task_client(
            "task4",
            client,
            command,
            {
                "start": "Task-4-Service '/task4/start_navigation' ist nicht erreichbar.",
                "stop": "Task-4-Service '/task4/stop_navigation' ist nicht erreichbar.",
                "reset": "Task-4-Service '/task4/reset' ist nicht erreichbar.",
            }[command],
            "task4_result",
            websocket,
        )
    def _odom_callback(self, message: Odometry) -> None:
        pose = message.pose.pose
        q = pose.orientation
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        theta = math.atan2(siny_cosp, cosy_cosp)

        frame_id = message.header.frame_id or "odom"
        child_frame_id = message.child_frame_id or "base_link"
        payload = {
            "type": "task4_robot_pose",
            "frame_id": frame_id,
            "child_frame_id": child_frame_id,
            "x": float(pose.position.x),
            "y": float(pose.position.y),
            "theta": float(theta),
        }
        self._latest_task4_robot_pose = payload
        self._schedule_broadcast(payload)

    def _task4_plan_callback(self, message: RosPath) -> None:
        poses = []
        original_count = len(message.poses)
        step = max(1, original_count // 1000)

        for pose_stamped in message.poses[::step]:
            pose = pose_stamped.pose
            q = pose.orientation
            siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
            cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            theta = math.atan2(siny_cosp, cosy_cosp)
            poses.append({
                "x": float(pose.position.x),
                "y": float(pose.position.y),
                "theta": float(theta),
            })

        payload = {
            "type": "task4_plan",
            "frame_id": message.header.frame_id,
            "pose_count": original_count,
            "displayed_pose_count": len(poses),
            "poses": poses,
        }
        self._latest_task4_plan = payload
        self._schedule_broadcast(payload)


    def _task4_polygon_callback(self, message: Marker) -> None:
        if message.ns and message.ns != "coverage_polygon":
            return

        points = [
            {
                "x": float(point.x),
                "y": float(point.y),
            }
            for point in message.points
        ]

        if len(points) > 1:
            first = points[0]
            last = points[-1]
            if math.isclose(first["x"], last["x"], abs_tol=1e-9) and math.isclose(
                first["y"],
                last["y"],
                abs_tol=1e-9,
            ):
                points = points[:-1]

        if len(points) < 3:
            return

        payload = {
            "type": "task4_polygon",
            "frame_id": message.header.frame_id,
            "points": points,
        }
        self._latest_task4_polygon = payload
        self._schedule_broadcast(payload)

    def _start_navigation_task(
        self,
        task: str,
        result_type: str,
        websocket: WebSocket,
        config: dict[str, str],
    ) -> None:
        if self._active_source != TASK_SOURCE:
            self._schedule_send(websocket, {
                "type": result_type,
                "task": task,
                "command": "start",
                "success": False,
                "message": "Tasks muss in der cmd_vel-Weiche als aktive Quelle ausgewählt sein.",
                **self._runtime_fields(),
            })
            return

        ok_pattern, pattern = self._validate_pattern(config.get("pattern", ""))
        if not ok_pattern:
            self._schedule_send(websocket, {
                "type": result_type,
                "task": task,
                "command": "start",
                "success": False,
                "message": pattern,
                **self._runtime_fields(),
            })
            return

        ok, message = self._begin_task_start(task)
        if not ok:
            self._schedule_send(websocket, {
                "type": result_type,
                "task": task,
                "command": "start",
                "success": False,
                "message": message,
                **self._runtime_fields(),
            })
            return

        if not self._service_ready(self._start_navigation_client):
            self._finish_task_start(task, False)
            self._schedule_send(websocket, {
                "type": result_type,
                "task": task,
                "command": "start",
                "success": False,
                "message": "StartNavigation-Service '/start_navigation' ist nicht erreichbar.",
                **self._runtime_fields(),
            })
            return

        request = StartNavigation.Request()
        request.pattern = pattern
        request.carefulness = self._normalize_carefulness(config.get("carefulness", "high"))
        request.model_path = "" if task == "task1" else str(config.get("model_path", "")).strip()
        future = self._start_navigation_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                success = bool(response.success)
                self._finish_task_start(task, success)
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": "start",
                    "success": success,
                    "message": response.message,
                    "pattern": request.pattern,
                    "carefulness": request.carefulness,
                    **({"model_path": request.model_path} if request.model_path else {}),
                    **self._runtime_fields(),
                }
            except Exception as exc:
                self._finish_task_start(task, False)
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": "start",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                    **self._runtime_fields(),
                }
            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def _trigger_task_client(
        self,
        task: str,
        client: Any,
        command: str,
        unavailable_message: str,
        result_type: str,
        websocket: WebSocket,
    ) -> None:
        if command == "start":
            ok, message = self._begin_task_start(task)
            if not ok:
                self._schedule_send(
                    websocket,
                    {
                        "type": result_type,
                        "task": task,
                        "command": command,
                        "success": False,
                        "message": message,
                        **self._runtime_fields(),
                    },
                )
                return

        if not self._service_ready(client):
            if command == "start":
                self._finish_task_start(task, False)
            self._schedule_send(
                websocket,
                {
                    "type": result_type,
                    "task": task,
                    "command": command,
                    "success": False,
                    "message": unavailable_message,
                    **self._runtime_fields(),
                },
            )
            return

        future = client.call_async(Trigger.Request())

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                success = bool(response.success)
                if command == "start":
                    self._finish_task_start(task, success)
                elif command == "stop":
                    self._finish_task_stop(task, success)
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": command,
                    "success": success,
                    "message": response.message,
                    **self._runtime_fields(),
                }
            except Exception as exc:
                if command == "start":
                    self._finish_task_start(task, False)
                payload = {
                    "type": result_type,
                    "task": task,
                    "command": command,
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                    **self._runtime_fields(),
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def _trigger_task4_client(
        self,
        client: Any,
        command: str,
        unavailable_message: str,
        websocket: WebSocket,
    ) -> None:
        if not self._service_ready(client):
            self._schedule_send(
                websocket,
                {
                    "type": "task4_result",
                    "command": command,
                    "success": False,
                    "message": unavailable_message,
                },
            )
            return

        future = client.call_async(Trigger.Request())

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "task4_result",
                    "command": command,
                    "success": bool(response.success),
                    "message": response.message,
                }
            except Exception as exc:
                payload = {
                    "type": "task4_result",
                    "command": command,
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def _active_source_callback(self, message: String) -> None:
        self._active_source = message.data
        self._schedule_broadcast(self.status_payload())

    def _publish_timer_callback(self) -> None:
        now = time.monotonic()

        with self._command_lock:
            timed_out = now - self._last_cmd_time > self._timeout_s
            if timed_out:
                linear_x = 0.0
                angular_z = 0.0
                self._moving_command_active = False
            else:
                linear_x = self._latest_linear_x
                angular_z = self._latest_angular_z

        self._publish_twist(linear_x, angular_z)

    def _schedule_broadcast(self, payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast(payload),
            self._loop,
        )

    def _schedule_send(
        self,
        websocket: WebSocket,
        payload: dict[str, Any],
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            websocket.send_text(json.dumps(payload)),
            self._loop,
        )


manager = ConnectionManager()
ros_node: WebTeleopNode | None = None
executor: MultiThreadedExecutor | None = None
spin_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global ros_node, executor, spin_thread

    rclpy.init(args=None)

    ros_node = WebTeleopNode(
        asyncio.get_running_loop(),
        manager,
    )

    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(ros_node)

    spin_thread = threading.Thread(
        target=executor.spin,
        daemon=True,
    )
    spin_thread.start()

    yield

    if ros_node is not None:
        ros_node.stop()

    if executor is not None:
        executor.shutdown()

    if ros_node is not None:
        ros_node.destroy_node()

    if rclpy.ok():
        rclpy.shutdown()

    if spin_thread is not None:
        spin_thread.join(timeout=1.0)


app = FastAPI(lifespan=lifespan)


def index_path() -> Path:
    return (
        Path(get_package_share_directory("web_teleop"))
        / "static"
        / "index.html"
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(index_path())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)

    node = ros_node
    if node is not None:
        await websocket.send_text(json.dumps(node.status_payload()))
        await websocket.send_text(json.dumps(node.available_models_payload()))
        if node._latest_task4_polygon is not None:
            await websocket.send_text(json.dumps(node._latest_task4_polygon))
        if node._latest_task4_robot_pose is not None:
            await websocket.send_text(json.dumps(node._latest_task4_robot_pose))
        if node._latest_task4_plan is not None:
            await websocket.send_text(json.dumps(node._latest_task4_plan))

    try:
        while True:
            data = json.loads(await websocket.receive_text())

            node = ros_node
            if node is None:
                continue

            message_type = data.get("type", "cmd_vel")

            if message_type == "cmd_vel":
                node.update_cmd_vel(
                    float(data.get("v", 0.0)),
                    float(data.get("w", 0.0)),
                )

            elif message_type == "select_source":
                node.stop()
                node.select_source(
                    str(data.get("source", "")),
                    websocket,
                )

            elif message_type == "request_selector_status":
                await websocket.send_text(
                    json.dumps(node.status_payload())
                )

            elif message_type == "configure_task_navigation":
                node.configure_task_navigation(
                    str(data.get("task", "")),
                    str(data.get("pattern", "")),
                    str(data.get("carefulness", "high")),
                    str(data.get("model_path", "")),
                    websocket,
                )

            elif message_type == "request_navigation_status":
                node.request_navigation_status(websocket)

            elif message_type == "reset_navigation":
                node.reset_navigation(
                    websocket,
                    str(data.get("task", "task1")),
                )

            elif message_type == "reset_slam_map":
                node.reset_slam_map(websocket)

            elif message_type == "reference_ptu":
                node.reference_ptu(websocket)

            elif message_type == "set_front_scan_profile":
                node.set_front_scan_profile(
                    str(data.get("profile", "")),
                    websocket,
                )

            elif message_type == "set_task4_polygon":
                raw_coords = data.get("polygon_coords", [])
                coords = raw_coords if isinstance(raw_coords, list) else []
                node.set_task4_polygon(coords, websocket)

            elif message_type == "trigger_task4_planning":
                node.trigger_task4_planning(websocket)

            elif message_type == "task_command":
                node.trigger_generic_task_command(
                    str(data.get("task", "")),
                    str(data.get("command", "")),
                    websocket,
                    str(data.get("pattern", "")),
                    str(data.get("carefulness", "high")),
                    str(data.get("model_path", "")),
                )

            elif message_type == "task4_command":
                node.trigger_task4_command(
                    str(data.get("command", "")),
                    websocket,
                )

            elif message_type == "navigation_command":
                node.trigger_navigation(
                    str(data.get("command", "")),
                    bool(data.get("confirmed", False)),
                    websocket,
                    str(data.get("pattern", "")),
                    str(data.get("carefulness", "high")),
                )

    except (
        WebSocketDisconnect,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ):
        if node is not None:
            node.stop()

    finally:
        await manager.disconnect(websocket)


def main() -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
