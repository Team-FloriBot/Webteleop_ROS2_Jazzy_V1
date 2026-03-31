import asyncio
import json
import signal
import time
import os
from pathlib import Path

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ament_index_python.packages import get_package_share_directory


class CmdVelBridge(Node):
    def __init__(self):
        super().__init__("cmdvel_web_bridge")

        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("max_linear", 0.25)   # m/s
        self.declare_parameter("max_angular", 0.9)   # rad/s
        self.declare_parameter("timeout_s", 0.3)     # s (Deadman)

        topic = self.get_parameter("cmd_vel_topic").get_parameter_value().string_value
        self.max_linear = float(self.get_parameter("max_linear").value)
        self.max_angular = float(self.get_parameter("max_angular").value)
        self.timeout_s = float(self.get_parameter("timeout_s").value)

        self.publisher_ = self.create_publisher(Twist, topic, 10)

        self._last_rx = time.monotonic()
        self._v = 0.0
        self._w = 0.0

        # 20 Hz
        self.create_timer(0.05, self._timer_cb)

    def update(self, v: float, w: float):
        v = max(-self.max_linear, min(self.max_linear, v))
        w = max(-self.max_angular, min(self.max_angular, w))
        self._v = v
        self._w = w
        self._last_rx = time.monotonic()

    def _timer_cb(self):
        msg = Twist()
        if (time.monotonic() - self._last_rx) > self.timeout_s:
            msg.linear.x = 0.0
            msg.angular.z = 0.0
        else:
            msg.linear.x = float(self._v)
            msg.angular.z = float(self._w)
        self.publisher_.publish(msg)


async def ros_spin(node: Node, stop_event: asyncio.Event):
    try:
        while rclpy.ok() and not stop_event.is_set():
            rclpy.spin_once(node, timeout_sec=0.0)
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        pass


def build_app(bridge: CmdVelBridge) -> FastAPI:
    # 🔧 ROS2-konformer Pfad zum share-Verzeichnis
    pkg_share = Path(get_package_share_directory("web_teleop"))
    static_dir = pkg_share / "static"
    index_file = static_dir / "index.html"

    if not static_dir.is_dir():
        raise RuntimeError(f"Static directory not found: {static_dir}")
    if not index_file.is_file():
        raise RuntimeError(f"Index file not found: {index_file}")

    app = FastAPI()

    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir)),
        name="static",
    )

    @app.get("/")
    def index():
        return FileResponse(str(index_file))

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_text()
                obj = json.loads(data)
                bridge.update(
                    float(obj.get("v", 0.0)),
                    float(obj.get("w", 0.0)),
                )
                await ws.send_text('{"ok": true}')
        except (WebSocketDisconnect, json.JSONDecodeError, ValueError):
            return

    return app


async def main_async():
    import uvicorn

    rclpy.init()
    bridge = CmdVelBridge()
    app = build_app(bridge)

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown():
        stop_event.set()
        server.should_exit = True

    try:
        loop.add_signal_handler(signal.SIGINT, request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, request_shutdown)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: request_shutdown())
        signal.signal(signal.SIGTERM, lambda *_: request_shutdown())

    ros_task = asyncio.create_task(ros_spin(bridge, stop_event))
    web_task = asyncio.create_task(server.serve())

    try:
        done, pending = await asyncio.wait(
            {ros_task, web_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        request_shutdown()

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    finally:
        try:
            bridge.destroy_node()
        except Exception:
            pass

        try:
            if rclpy.ok():
                rclpy.shutdown()
        except Exception:
            pass


def main():
    asyncio.run(main_async())
