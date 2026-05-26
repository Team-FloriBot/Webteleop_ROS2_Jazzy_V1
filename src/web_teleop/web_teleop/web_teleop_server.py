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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from geometry_msgs.msg import Twist
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
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

        self._watchdog = self.create_timer(
            0.05,
            self._watchdog_callback,
        )

        self.get_logger().info(
            f"Webteleop publiziert Fahrbefehle auf '{self._cmd_vel_topic}'."
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

    def publish_cmd_vel(self, linear_x: float, angular_z: float) -> None:
        linear_x = max(
            -self._max_linear,
            min(self._max_linear, float(linear_x)),
        )
        angular_z = max(
            -self._max_angular,
            min(self._max_angular, float(angular_z)),
        )

        message = Twist()
        message.linear.x = linear_x
        message.angular.z = angular_z

        self._publisher.publish(message)

        self._last_cmd_time = time.monotonic()
        self._moving_command_active = linear_x != 0.0 or angular_z != 0.0

    def stop(self) -> None:
        self.publish_cmd_vel(0.0, 0.0)
        self._moving_command_active = False

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

    def _active_source_callback(self, message: String) -> None:
        self._active_source = message.data
        self._schedule_broadcast(self.status_payload())

    def _watchdog_callback(self) -> None:
        if (
            self._moving_command_active
            and time.monotonic() - self._last_cmd_time > self._timeout_s
        ):
            self.stop()

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
                node.publish_cmd_vel(
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
