# Web Teleop für ROS 2 Jazzy

Browserbasierte Teleoperation für einen mobilen Roboter unter **ROS 2 Jazzy**.  
Das Paket `web_teleop` stellt eine Weboberfläche mit virtuellem Joystick bereit, publiziert Geschwindigkeitsbefehle als `geometry_msgs/msg/Twist` und ermöglicht die Umschaltung der aktiven Fahrquelle. Zusätzlich ist eine Bedienoberfläche für die Task-1-Navigation des Field Robot Event 2026 integriert.

## Funktionsumfang

- Weboberfläche für Smartphone, Tablet und Desktop-Browser
- Virtueller Joystick zur manuellen Vorgabe von Linear- und Winkelgeschwindigkeit
- Einstellbare Maximalgeschwindigkeiten im Browser
- WebSocket-Kommunikation zwischen Frontend und ROS-2-Backend
- Veröffentlichung von `Twist`-Nachrichten mit 20 Hz
- Deadman-Timeout: automatische Ausgabe von `v = 0` und `ω = 0`, wenn keine aktuellen Fahrbefehle empfangen werden
- Auswahl der aktiven Fahrquelle über einen externen `cmd_vel_selector`
- Bedienung von Task 1: Fahrmuster setzen, starten, pausieren, fortsetzen und stoppen
- Sofortiger Stopp beim Wechsel der Fahrquelle oder beim Verlust einer Bedienverbindung

## Systemübersicht

```text
Browser
  │  HTTP / WebSocket
  ▼
web_teleop_server  ─────────────► /cmd_vel/webteleop    (geometry_msgs/msg/Twist)
  │
  ├─────────────────────────────► /cmd_vel_selector/select_source
  ◄────────────────────────────── /cmd_vel_selector/active_source
  │
  ├─────────────────────────────► /set_navigation_pattern
  ├─────────────────────────────► /get_navigation_status
  ├─────────────────────────────► /start_navigation
  ├─────────────────────────────► /pause_navigation
  ├─────────────────────────────► /resume_navigation
  └─────────────────────────────► /stop_navigation
```

Der Webteleop-Node erzeugt die Befehle für die Quelle `webteleop`. Das Zusammenführen beziehungsweise Freigeben der Fahrquellen erfolgt nicht in diesem Paket, sondern durch den externen `cmd_vel_selector`.

## Verzeichnisstruktur

```text
Webteleop_ROS2_Jazzy_V1-main/
└── src/
    └── web_teleop/
        ├── launch/
        │   └── web_teleop.launch.py
        ├── resource/
        │   └── web_teleop
        ├── web_teleop/
        │   ├── __init__.py
        │   ├── static/
        │   │   └── index.html
        │   └── web_teleop_server.py
        ├── package.xml
        ├── setup.cfg
        └── setup.py
```

## Voraussetzungen

### Software

- Ubuntu 24.04
- ROS 2 Jazzy Jalisco
- Python 3
- `colcon`
- Ein ROS-2-Workspace, in dem die abhängigen Pakete verfügbar sind

### ROS-2-Abhängigkeiten

Das Paket deklariert folgende ROS-2-Abhängigkeiten:

- `rclpy`
- `geometry_msgs`
- `std_msgs`
- `std_srvs`
- `ament_index_python`
- `cmd_vel_selector`
- `fre2026_task_interfaces`

`cmd_vel_selector` und `fre2026_task_interfaces` sind projektspezifische Pakete und müssen im selben Workspace oder in einer bereits gesourcten Installation verfügbar sein.

### Python-Abhängigkeiten

Der Server verwendet zusätzlich:

- `fastapi`
- `uvicorn`

Installation über die Paketverwaltung beziehungsweise `pip`:

```bash
python3 -m pip install fastapi uvicorn
```

## Installation und Build

Das Repository besitzt bereits die Struktur eines ROS-2-Workspaces mit dem Paket unter `src/`.

```bash
cd ~/ros2_ws
unzip Webteleop_ROS2_Jazzy_V1-main.zip
cp -r Webteleop_ROS2_Jazzy_V1-main/src/web_teleop src/
```

Abhängigkeiten auflösen und Workspace bauen:

```bash
cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

Sind `cmd_vel_selector` oder `fre2026_task_interfaces` nicht über `rosdep` verfügbar, müssen deren Quellpakete vor dem Build ebenfalls in `~/ros2_ws/src/` liegen.

## Starten

### Start über Launch-Datei

```bash
cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash
ros2 launch web_teleop web_teleop.launch.py
```

Anschließend ist die Weboberfläche lokal unter folgender Adresse erreichbar:

```text
http://localhost:8000
```

Für ein Smartphone oder Tablet im selben Netzwerk die IP-Adresse des ROS-Rechners verwenden:

```text
http://<IP-DES-ROS-RECHNERS>:8000
```

Beispiel zur Ermittlung der IP-Adresse:

```bash
hostname -I
```

### Start über ausführbaren Node

```bash
ros2 run web_teleop web_teleop_server
```

Beim direkten Start werden die Standardparameter des Nodes verwendet.

## Launch-Parameter

| Parameter | Standardwert | Einheit | Beschreibung |
| --- | ---: | --- | --- |
| `cmd_vel_topic` | `/cmd_vel/webteleop` | – | Zieltopic für manuelle Fahrbefehle |
| `max_linear` | `1.0` | m/s | Backend-Begrenzung für `linear.x` |
| `max_angular` | `0.9` | rad/s | Backend-Begrenzung für `angular.z` |
| `timeout_s` | `0.3` | s | Deadman-Timeout ohne neue Web-Befehle |

Beispiel mit reduzierten Grenzwerten:

```bash
ros2 launch web_teleop web_teleop.launch.py \
  max_linear:=0.30 \
  max_angular:=0.50 \
  timeout_s:=0.20
```

## Bedienung der Weboberfläche

### Manuelle Fahrt

1. Weboberfläche im Browser öffnen.
2. Über das Menü die Fahrquelle **Webteleop** auswählen.
3. Maximale lineare und Winkelgeschwindigkeit mit den Schiebereglern begrenzen.
4. In der Bedienfläche drücken beziehungsweise tippen und den virtuellen Joystick auslenken.
5. Beim Loslassen wird unmittelbar ein Stoppbefehl übertragen.

Die Joystick-Steuerung ist nur aktiv, wenn `webteleop` als aktive Fahrquelle rückgemeldet wird.

### Task-1-Navigation

Im Einstellungsmenü kann die Fahrquelle **Task 1** ausgewählt werden. Anschließend stehen folgende Aktionen zur Verfügung:

- Fahrmuster setzen, beispielsweise `1L 2R`
- Navigation starten
- Navigation pausieren
- Navigation fortsetzen
- Navigation stoppen

Vor dem Start muss **Task 1** als Fahrquelle aktiv sein. Das Starten und Pausieren verlangt in der Benutzeroberfläche jeweils eine explizite Bestätigung.

## ROS-2-Schnittstellen

### Publiziertes Topic

| Topic | Nachrichtentyp | Beschreibung |
| --- | --- | --- |
| `/cmd_vel/webteleop` | `geometry_msgs/msg/Twist` | Manuelle Geschwindigkeitsbefehle aus dem Web-Joystick |

Das Topic kann über den Parameter `cmd_vel_topic` geändert werden.

### Abonniertes Topic

| Topic | Nachrichtentyp | Beschreibung |
| --- | --- | --- |
| `/cmd_vel_selector/active_source` | `std_msgs/msg/String` | Aktuell freigegebene Fahrquelle |

Die Statussubscription verwendet zuverlässige Übertragung (`RELIABLE`) und `TRANSIENT_LOCAL`, damit ein zuletzt publizierter Quellenstatus nach dem Start verfügbar sein kann.

### Verwendete Services

| Service | Servicetyp | Funktion |
| --- | --- | --- |
| `/cmd_vel_selector/select_source` | `cmd_vel_selector/srv/SelectSource` | Aktive Fahrquelle auswählen |
| `/set_navigation_pattern` | `fre2026_task_interfaces/srv/SetNavigationPattern` | Fahrmuster für Task 1 setzen |
| `/get_navigation_status` | `fre2026_task_interfaces/srv/GetNavigationStatus` | Status der Navigation abfragen |
| `/start_navigation` | `std_srvs/srv/Trigger` | Navigation starten |
| `/pause_navigation` | `std_srvs/srv/Trigger` | Navigation pausieren |
| `/resume_navigation` | `std_srvs/srv/Trigger` | Navigation fortsetzen |
| `/stop_navigation` | `std_srvs/srv/Trigger` | Navigation stoppen |

### Fahrquellen

Das Frontend unterstützt die folgenden Quellenbezeichner:

| Quellen-ID | Anzeige |
| --- | --- |
| `none` | Stopp / keine Quelle |
| `webteleop` | Webteleop |
| `task1` | Task 1 |
| `task2` | Task 2 |
| `task3` | Task 3 |
| `task4` | Task 4 |
| `task5` | Task 5 |

Die tatsächlichen Topics und Nodes für `task1` bis `task5` müssen durch die restliche Roboter-Software bereitgestellt werden.

## Sicherheitsmechanismen

- Der Node begrenzt eingehende Geschwindigkeitswerte serverseitig auf `max_linear` und `max_angular`.
- Fahrbefehle werden zyklisch mit 20 Hz publiziert.
- Nach Ablauf von `timeout_s` ohne neuen Steuerbefehl publiziert der Node ausschließlich Nullgeschwindigkeiten.
- Bei WebSocket-Abbruch wird ein Stopp ausgeführt.
- Beim Wechsel der Fahrquelle wird zunächst ein Stoppbefehl publiziert.
- Das Frontend deaktiviert den Joystick, solange `webteleop` nicht als aktive Quelle gemeldet ist.

Trotz dieser Maßnahmen muss die reale Roboterplattform über unabhängige Sicherheitsfunktionen verfügen, insbesondere Not-Halt, Begrenzung der Fahrgeschwindigkeit und eine sichere Freigabelogik.

## Diagnose

### Prüfen, ob der Node läuft

```bash
ros2 node list | grep web_teleop_server
```

### Ausgegebene Fahrbefehle überwachen

```bash
ros2 topic echo /cmd_vel/webteleop
```

### Aktive Fahrquelle überwachen

```bash
ros2 topic echo /cmd_vel_selector/active_source
```

### Verfügbare Services prüfen

```bash
ros2 service list | grep -E "cmd_vel_selector|navigation|set_navigation_pattern"
```

### Typische Fehlerbilder

| Fehlerbild | Mögliche Ursache | Maßnahme |
| --- | --- | --- |
| Weboberfläche nicht erreichbar | Server nicht gestartet oder Port 8000 blockiert | Node starten und Netzwerk-/Firewall-Einstellungen prüfen |
| Anzeige bleibt `Offline` | WebSocket-Verbindung nicht verfügbar | Erreichbarkeit des ROS-Rechners und Browseradresse prüfen |
| Quellenwahl schlägt fehl | `/cmd_vel_selector/select_source` ist nicht verfügbar | `cmd_vel_selector` starten und Service prüfen |
| Task-1-Befehle schlagen fehl | Navigationsservices fehlen | Task-1-Navigation beziehungsweise zugehörigen Node starten |
| Roboter bewegt sich nicht | `webteleop` nicht freigegeben oder nachgeschaltete Multiplexierung fehlt | Aktive Quelle und Ausgang des Selektors prüfen |

## Lizenz

Dieses Paket ist unter der **Apache License 2.0** lizenziert. Die Lizenzangabe entspricht der Paketdeklaration in `package.xml` und `setup.py`.

## Autor

Timo Zimmermann  
Hochschule Heilbronn  
