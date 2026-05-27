"""ROS 2 backed WebTeleop server with cmd_vel source selection."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import rclpy
from ament_index_python.packages import get_package_share_directory
from cmd_vel_selector.srv import SelectSource
from fre2026_task_interfaces.srv import GetNavigationStatus, SetNavigationPattern
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from geometry_msgs.msg import Twist
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger
import uvicorn


VALID_SOURCES = ("none", "webteleop", "task1", "task2", "task3", "task4", "task5")
SOURCE_LABELS = {
    "none": "Stopp / keine Quelle",
    "webteleop": "Webteleop",
    "task1": "Task 1",
    "task2": "Task 2",
    "task3": "Task 3",
    "task4": "Task 4",
    "task5": "Task 5",
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
            self.declare_parameter("timeout_s", 0.3).value
        )

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
        self._set_pattern_client = self.create_client(
            SetNavigationPattern,
            "/set_navigation_pattern",
        )
        self._get_navigation_status_client = self.create_client(
            GetNavigationStatus,
            "/get_navigation_status",
        )
        self._start_navigation_client = self.create_client(
            Trigger,
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

        self._publish_timer = self.create_timer(
            0.05,
            self._publish_timer_callback,
        )

        self.get_logger().info(
            f"Webteleop publiziert Fahrbefehle mit 20 Hz auf '{self._cmd_vel_topic}'."
        )

    @property
    def active_source(self) -> str:
        return self._active_source

    def status_payload(self) -> dict[str, Any]:
        return {
            "type": "selector_status",
            "active_source": self._active_source,
            "active_source_label": SOURCE_LABELS.get(
                self._active_source,
                self._active_source,
            ),
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

        # Publish the stop immediately; the 20 Hz timer then keeps publishing zero.
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

        if not self._select_client.service_is_ready():
            self._select_client.wait_for_service(timeout_sec=0.2)

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
                }
            except Exception as exc:  # pragma: no cover
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
            client.wait_for_service(timeout_sec=0.2)
        return bool(client.service_is_ready())

    def set_navigation_pattern(self, pattern: str, websocket: WebSocket) -> None:
        if not self._service_ready(self._set_pattern_client):
            self._schedule_send(
                websocket,
                {
                    "type": "task_result",
                    "command": "set_pattern",
                    "success": False,
                    "message": "SetNavigationPattern-Service ist nicht erreichbar.",
                },
            )
            return

        request = SetNavigationPattern.Request()
        request.pattern = pattern
        future = self._set_pattern_client.call_async(request)

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "task_result",
                    "command": "set_pattern",
                    "success": bool(response.success),
                    "message": response.message,
                    "accepted_pattern": response.accepted_pattern,
                    "mission_state": response.mission_state,
                    "can_start": bool(response.can_start),
                    "can_resume": bool(response.can_resume),
                }
            except Exception as exc:  # pragma: no cover
                payload = {
                    "type": "task_result",
                    "command": "set_pattern",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def request_navigation_status(self, websocket: WebSocket) -> None:
        if not self._service_ready(self._get_navigation_status_client):
            self._schedule_send(
                websocket,
                {
                    "type": "navigation_status",
                    "success": False,
                    "message": "GetNavigationStatus-Service ist nicht erreichbar.",
                },
            )
            return

        future = self._get_navigation_status_client.call_async(
            GetNavigationStatus.Request()
        )

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "navigation_status",
                    "success": bool(response.success),
                    "message": response.message,
                    "mission_state": response.mission_state,
                    "pattern_loaded": bool(response.pattern_loaded),
                    "active_pattern": response.active_pattern,
                    "active_step_index": int(response.active_step_index),
                    "total_steps": int(response.total_steps),
                    "active_step": response.active_step,
                    "can_set_pattern": bool(response.can_set_pattern),
                    "can_start": bool(response.can_start),
                    "can_pause": bool(response.can_pause),
                    "can_resume": bool(response.can_resume),
                    "can_abort": bool(response.can_abort),
                }
            except Exception as exc:  # pragma: no cover
                payload = {
                    "type": "navigation_status",
                    "success": False,
                    "message": f"Serviceaufruf fehlgeschlagen: {exc}",
                }

            self._schedule_send(websocket, payload)

        future.add_done_callback(finish)

    def trigger_navigation(
        self,
        command: str,
        confirmed: bool,
        websocket: WebSocket,
    ) -> None:
        confirmation_required = command in {"start", "pause"}
        if confirmation_required and not confirmed:
            self._schedule_send(
                websocket,
                {
                    "type": "task_result",
                    "command": command,
                    "success": False,
                    "message": "Befehl verworfen: explizite Bestätigung fehlt.",
                },
            )
            return

        clients = {
            "start": self._start_navigation_client,
            "stop": self._stop_navigation_client,
            "pause": self._pause_navigation_client,
            "resume": self._resume_navigation_client,
        }
        client = clients.get(command)
        if client is None:
            self._schedule_send(
                websocket,
                {
                    "type": "task_result",
                    "command": command,
                    "success": False,
                    "message": "Unbekannter Navigationsbefehl.",
                },
            )
            return

        if not self._service_ready(client):
            self._schedule_send(
                websocket,
                {
                    "type": "task_result",
                    "command": command,
                    "success": False,
                    "message": f"Navigation-Service '{command}' ist nicht erreichbar.",
                },
            )
            return

        future = client.call_async(Trigger.Request())

        def finish(done_future: Any) -> None:
            try:
                response = done_future.result()
                payload = {
                    "type": "task_result",
                    "command": command,
                    "success": bool(response.success),
                    "message": response.message,
                }
            except Exception as exc:  # pragma: no cover
                payload = {
                    "type": "task_result",
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

            elif message_type == "set_navigation_pattern":
                node.set_navigation_pattern(
                    str(data.get("pattern", "")),
                    websocket,
                )

            elif message_type == "request_navigation_status":
                node.request_navigation_status(websocket)

            elif message_type == "navigation_command":
                node.trigger_navigation(
                    str(data.get("command", "")),
                    bool(data.get("confirmed", False)),
                    websocket,
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
