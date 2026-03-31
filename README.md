# FloriBot1.0_ROS2
Dieses Repository wird für die Entwicklung der Base vom FloriBot1.0 auf ROS2 genutzt

# Klonen des Repository
```
git clone https://github.com/Team-FloriBot/FloriBot1.0_ROS2.git
```
```
cd FloriBot1.0_ROS2
```


# Abhaengigkeiten installieren
```
rosdep install -i --from-path src --rosdistro jazzy -y
```

# Build workspace
```
colcon build
```

# Source
open new terminal
```
source /opt/ros/jazzy/setup.bash
```
```
cd ~/FloriBot1.0_ROS2
```
```
source install/local_setup.bash
```

