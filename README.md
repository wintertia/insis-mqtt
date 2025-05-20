# Integrasi Sistem 2025 - MQTT: Simple Plant Monitor

## Kelompok

| Name                    | NRP        |
|-------------------------|------------|
| Amoes Noland            | 5027231028 |
| Dionisius Marcell Putra Indranto | 5027231044 |
| Tio Axellino Irin | 5027231065 |

## Usage

### Prerequisite

* Have Python and install important packages:
```sh
pip install flask paho-mqtt
```

* Create an ENV file with this template:
```env
# MQTT Credentials
MQTT_BROKER_HOST="your_cluster_id.s1.eu.hivemq.cloud"
MQTT_BROKER_PORT="8883"
MQTT_USERNAME="your_username"
MQTT_PASSWORD="your_password"
```

### Starting

#### Publisher:
```sh
python plant_monitor.py
```

#### Subscriber/Frontend:
```sh
python plant_subscriber.py
```