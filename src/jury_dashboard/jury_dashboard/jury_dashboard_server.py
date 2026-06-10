#!/usr/bin/env python3

"""ROS 2 backend and web server for the FRE 2026 jury dashboard."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any

from ament_index_python.packages import get_package_share_directory
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import FileResponse
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
import uvicorn


SUPPORTED_LABELS = {
    "diseased",
    "diseased_plant",
    "unhealthy",
    "unhealthy_plant",
    "bee",
    "beetle",
    "aphid",
    "pest",
    "butterfly",
    "neutral",
}

LABEL_ALIASES = {
    "diseased_plant": "diseased",
    "unhealthy": "diseased",
    "unhealthy_plant": "diseased",
    "beetle": "pest",
    "aphid": "pest",
    "neutral": "butterfly",
}

DISPLAY_CONFIGURATION = {
    "diseased": {
        "task": 2,
        "title": "DISEASED PLANT",
        "subtitle": "Unhealthy plant detected",
        "signal": "orange",
    },
    "bee": {
        "task": 3,
        "title": "BEE",
        "subtitle": "Beneficial insect",
        "signal": "green",
    },
    "pest": {
        "task": 3,
        "title": "PEST",
        "subtitle": "Pest detected",
        "signal": "red",
    },
    "butterfly": {
        "task": 3,
        "title": "BUTTERFLY",
        "subtitle": "Neutral insect",
        "signal": "yellow",
    },
}

TEXT_LABEL_PATTERNS = (
    (
        "diseased",
        re.compile(
            r"\b(diseased|unhealthy|diseased plant|unhealthy plant)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bee",
        re.compile(
            r"\bbee\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pest",
        re.compile(
            r"\b(pest|beetle|aphid)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "butterfly",
        re.compile(
            r"\b(butterfly|neutral)\b",
            re.IGNORECASE,
        ),
    ),
)


class DashboardState:
    """Thread-safe event storage and WebSocket distribution."""

    def __init__(self, maximum_events: int = 100) -> None:
        self._events: deque[dict[str, Any]] = deque(
            maxlen=maximum_events
        )
        self._event_counter = 0
        self._lock = threading.Lock()

        self._web_event_loop: asyncio.AbstractEventLoop | None = None
        self._client_queues: set[asyncio.Queue] = set()

    def set_web_event_loop(
        self,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Store the event loop used by the FastAPI server."""

        self._web_event_loop = event_loop

    def add_event(self, event: dict[str, Any]) -> None:
        """Store an event and send it to all connected clients."""

        with self._lock:
            self._event_counter += 1
            event["event_number"] = self._event_counter
            self._events.appendleft(event)

        event_loop = self._web_event_loop

        if event_loop is None or not event_loop.is_running():
            return

        asyncio.run_coroutine_threadsafe(
            self._broadcast_event(event),
            event_loop,
        )

    def get_events(self) -> list[dict[str, Any]]:
        """Return a copy of all stored events."""

        with self._lock:
            return list(self._events)

    def clear_events(self) -> None:
        """Remove all events and reset the event counter."""

        with self._lock:
            self._events.clear()
            self._event_counter = 0

    async def register_client(self) -> asyncio.Queue:
        """Register a WebSocket client."""

        queue: asyncio.Queue = asyncio.Queue(maxsize=25)
        self._client_queues.add(queue)
        return queue

    async def unregister_client(
        self,
        queue: asyncio.Queue,
    ) -> None:
        """Unregister a WebSocket client."""

        self._client_queues.discard(queue)

    async def _broadcast_event(
        self,
        event: dict[str, Any],
    ) -> None:
        """Place an event in every registered client queue."""

        unavailable_queues: list[asyncio.Queue] = []

        for queue in tuple(self._client_queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                unavailable_queues.append(queue)

        for queue in unavailable_queues:
            self._client_queues.discard(queue)


DASHBOARD_STATE = DashboardState()

APP = FastAPI(
    title="FRE 2026 Jury Dashboard",
    description=(
        "Read-only dashboard for Task 2 and Task 3 "
        "classification results."
    ),
    version="0.1.0",
)


def normalize_label(raw_label: str) -> str | None:
    """Normalize a detector or task label."""

    normalized_label = (
        raw_label
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )

    normalized_label = LABEL_ALIASES.get(
        normalized_label,
        normalized_label,
    )

    if normalized_label not in DISPLAY_CONFIGURATION:
        return None

    return normalized_label


def extract_label_from_text(text: str) -> str | None:
    """Extract a supported classification from a plain-text message."""

    for normalized_label, pattern in TEXT_LABEL_PATTERNS:
        if pattern.search(text):
            return normalized_label

    return None


def extract_side_from_text(text: str) -> str | None:
    """Extract left or right from a plain-text message."""

    lower_text = text.lower()

    if re.search(r"\bleft\b", lower_text):
        return "left"

    if re.search(r"\bright\b", lower_text):
        return "right"

    return None


def parse_optional_integer(value: Any) -> int | None:
    """Convert a value to int when possible."""

    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_optional_float(value: Any) -> float | None:
    """Convert a value to float when possible."""

    if value is None or value == "":
        return None

    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def create_event_from_json(
    payload: dict[str, Any],
    source_text: str,
) -> dict[str, Any] | None:
    """Create a dashboard event from a JSON object."""

    raw_label = (
        payload.get("label")
        or payload.get("classification")
        or payload.get("class")
        or payload.get("object_class")
    )

    if raw_label is None:
        return None

    label = normalize_label(str(raw_label))

    if label is None:
        return None

    side = payload.get("side")

    if side is not None:
        side = str(side).strip().lower()

        if side not in {"left", "right"}:
            side = None

    configuration = DISPLAY_CONFIGURATION[label]

    event = {
        "label": label,
        "task": configuration["task"],
        "title": configuration["title"],
        "subtitle": configuration["subtitle"],
        "signal": configuration["signal"],
        "side": side,
        "row": parse_optional_integer(payload.get("row")),
        "distance_m": parse_optional_float(
            payload.get("distance_m")
        ),
        "object_id": parse_optional_integer(
            payload.get("object_id")
        ),
        "station_id": parse_optional_integer(
            payload.get("station_id")
        ),
        "confidence": parse_optional_float(
            payload.get("confidence")
        ),
        "source_text": source_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return event


def create_event_from_text(
    source_text: str,
) -> dict[str, Any] | None:
    """Create a dashboard event from a plain-text message."""

    label = extract_label_from_text(source_text)

    if label is None:
        return None

    configuration = DISPLAY_CONFIGURATION[label]

    event = {
        "label": label,
        "task": configuration["task"],
        "title": configuration["title"],
        "subtitle": configuration["subtitle"],
        "signal": configuration["signal"],
        "side": extract_side_from_text(source_text),
        "row": None,
        "distance_m": None,
        "object_id": None,
        "station_id": None,
        "confidence": None,
        "source_text": source_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return event


def parse_classification_message(
    raw_message: str,
) -> dict[str, Any] | None:
    """Parse either a JSON classification or an existing audio phrase."""

    source_text = raw_message.strip()

    if not source_text:
        return None

    try:
        decoded_payload = json.loads(source_text)
    except json.JSONDecodeError:
        decoded_payload = None

    if isinstance(decoded_payload, dict):
        return create_event_from_json(
            decoded_payload,
            source_text,
        )

    return create_event_from_text(source_text)


class JuryDashboardNode(Node):
    """ROS 2 subscriber for classification results."""

    def __init__(self) -> None:
        super().__init__("jury_dashboard_server")

        self.declare_parameter(
            "classification_topic",
            "/classification_result",
        )
        self.declare_parameter(
            "web_host",
            "0.0.0.0",
        )
        self.declare_parameter(
            "web_port",
            8081,
        )

        self.classification_topic = (
            self.get_parameter("classification_topic")
            .get_parameter_value()
            .string_value
        )

        self.web_host = (
            self.get_parameter("web_host")
            .get_parameter_value()
            .string_value
        )

        self.web_port = (
            self.get_parameter("web_port")
            .get_parameter_value()
            .integer_value
        )

        self.classification_subscription = self.create_subscription(
            String,
            self.classification_topic,
            self.classification_callback,
            10,
        )

        self.get_logger().info(
            "Jury dashboard subscribes to "
            f"'{self.classification_topic}'."
        )

    def classification_callback(
        self,
        message: String,
    ) -> None:
        """Process a classification result."""

        event = parse_classification_message(message.data)

        if event is None:
            self.get_logger().debug(
                "Ignored non-classification message: "
                f"{message.data!r}"
            )
            return

        DASHBOARD_STATE.add_event(event)

        self.get_logger().info(
            "Jury event received: "
            f"task={event['task']}, "
            f"label={event['label']}, "
            f"side={event['side']}, "
            f"row={event['row']}, "
            f"distance_m={event['distance_m']}"
        )


PACKAGE_SHARE_DIRECTORY = Path(
    get_package_share_directory("jury_dashboard")
)

INDEX_FILE = (
    PACKAGE_SHARE_DIRECTORY
    / "static"
    / "index.html"
)


@APP.on_event("startup")
async def application_startup() -> None:
    """Store the FastAPI event loop for ROS-to-WebSocket transfer."""

    DASHBOARD_STATE.set_web_event_loop(
        asyncio.get_running_loop()
    )


@APP.get("/")
async def index() -> FileResponse:
    """Serve the jury dashboard."""

    return FileResponse(
        path=INDEX_FILE,
        media_type="text/html",
    )


@APP.get("/task2")
async def task_2_page() -> FileResponse:
    """Serve the dashboard with Task 2 selected."""

    return FileResponse(
        path=INDEX_FILE,
        media_type="text/html",
    )


@APP.get("/task3")
async def task_3_page() -> FileResponse:
    """Serve the dashboard with Task 3 selected."""

    return FileResponse(
        path=INDEX_FILE,
        media_type="text/html",
    )


@APP.get("/health")
async def health() -> dict[str, Any]:
    """Return the web server health state."""

    events = DASHBOARD_STATE.get_events()

    return {
        "status": "ok",
        "stored_event_count": len(events),
        "last_event": events[0] if events else None,
    }


@APP.get("/api/events")
async def get_events() -> dict[str, Any]:
    """Return all currently stored classification events."""

    return {
        "events": DASHBOARD_STATE.get_events(),
    }


@APP.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
) -> None:
    """Send classification events to a connected browser."""

    await websocket.accept()

    queue = await DASHBOARD_STATE.register_client()

    try:
        await websocket.send_json(
            {
                "type": "initial_state",
                "events": DASHBOARD_STATE.get_events(),
            }
        )

        while True:
            event = await queue.get()

            await websocket.send_json(
                {
                    "type": "classification",
                    "event": event,
                }
            )

    except WebSocketDisconnect:
        pass
    finally:
        await DASHBOARD_STATE.unregister_client(queue)


def run_web_server(
    host: str,
    port: int,
) -> None:
    """Run the FastAPI server in a separate thread."""

    uvicorn.run(
        APP,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


def main(args: list[str] | None = None) -> None:
    """Start ROS 2 and the dashboard web server."""

    rclpy.init(args=args)

    node = JuryDashboardNode()

    web_server_thread = threading.Thread(
        target=run_web_server,
        args=(
            node.web_host,
            node.web_port,
        ),
        name="jury-dashboard-web-server",
        daemon=True,
    )

    web_server_thread.start()

    node.get_logger().info(
        "Jury dashboard started at "
        f"http://{node.web_host}:{node.web_port}"
    )

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info(
            "Jury dashboard interrupted."
        )
    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
