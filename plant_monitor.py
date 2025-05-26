import paho.mqtt.client as mqtt
import paho.mqtt.properties as mqtt_properties  # For MQTTv5 properties
import time
import random
import json
import ssl
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- MQTT Configuration ---
BROKER_HOST = os.getenv("MQTT_BROKER_HOST")
BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT"))
USERNAME = os.getenv("MQTT_USERNAME")
PASSWORD = os.getenv("MQTT_PASSWORD")

# Basic validation
if not all([BROKER_HOST, USERNAME, PASSWORD]):
    print("CRITICAL: MQTT credentials are not fully set in environment variables or .env file.")
    print("Please ensure MQTT_BROKER_HOST, MQTT_USERNAME, and MQTT_PASSWORD are set.")
    exit(1)

# Topics
TOPIC_MOISTURE = "office/plant/moisture"
# For general status messages, config acks, etc.
TOPIC_STATUS = "office/plant/status"
TOPIC_ALERT = "office/plant/alert"
CLIENT_ID_PUB = "plant_monitor_sensor"

# New Topic for Web App's primary sensor status (online/offline/lwt)
TOPIC_APP_STATUS = "office/plant/app_status"

# New Topics for Request/Response and Ping/Pong
TOPIC_COMMAND_REQUEST = "office/plant/command/request"
TOPIC_COMMAND_RESPONSE = "office/plant/command/response"
TOPIC_PING_SENSOR = "office/plant/ping/sensor"  # Subscriber pings sensor
TOPIC_PONG_SENSOR = "office/plant/pong/sensor"  # Sensor pongs subscriber

# --- Last Will and Testament ---
# LWT will now be published to TOPIC_APP_STATUS
LWT_PAYLOAD = json.dumps(
    {"status": "offline_lwt", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})
LWT_QOS = 1

# Message Expiry Interval (e.g., 1 hour for status/alerts)
MESSAGE_EXPIRY_INTERVAL_SECONDS = 3600


def on_connect(client, userdata, flags, rc, properties=None):  # Added properties for MQTTv5
    if rc == 0:
        print(f"Publisher connected to MQTT Broker (rc: {rc})")
        # Publish an "online" status message to TOPIC_APP_STATUS for the web app
        online_payload = json.dumps(
            {"status": "online", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})

        props_app_status = mqtt_properties.Properties(
            mqtt_properties.PacketTypes.PUBLISH)
        props_app_status.MessageExpiryInterval = MESSAGE_EXPIRY_INTERVAL_SECONDS
        client.publish(TOPIC_APP_STATUS, online_payload, qos=1,
                       retain=True, properties=props_app_status)
        print(f"Published online status to {TOPIC_APP_STATUS} for web app")

        # Subscribe to command and ping topics
        client.subscribe(TOPIC_COMMAND_REQUEST, qos=1)
        print(f"Subscribed to {TOPIC_COMMAND_REQUEST}")
        client.subscribe(TOPIC_PING_SENSOR, qos=0)
        print(f"Subscribed to {TOPIC_PING_SENSOR}")
    else:
        print(f"Publisher failed to connect, return code {rc}\\n")


def on_message(client, userdata, msg):
    payload_str = msg.payload.decode()
    print(f"Publisher received message on topic '{msg.topic}': {payload_str}")

    if msg.topic == TOPIC_COMMAND_REQUEST:
        try:
            request_data = json.loads(payload_str)
            command = request_data.get("command")

            response_props = mqtt_properties.Properties(
                mqtt_properties.PacketTypes.PUBLISH)
            if msg.properties and msg.properties.CorrelationData:
                response_props.CorrelationData = msg.properties.CorrelationData

            response_payload = {}
            if command == "get_details":
                response_payload = {
                    "status": "success",
                    "sensor_id": CLIENT_ID_PUB,
                    "firmware_version": "1.0.2",
                    "last_calibration": time.time() - 7200  # Simulate 2 hours ago
                }
            else:
                response_payload = {"status": "error",
                                    "message": "Unknown command"}

            client.publish(TOPIC_COMMAND_RESPONSE, json.dumps(
                response_payload), qos=1, properties=response_props)
            print(
                f"Sent command response to {TOPIC_COMMAND_RESPONSE}: {response_payload}")

        except json.JSONDecodeError:
            print(f"Error decoding command JSON: {payload_str}")
        except Exception as e:
            print(f"Error processing command: {e}")

    elif msg.topic == TOPIC_PING_SENSOR:
        # Echo back the payload for pong
        pong_props = mqtt_properties.Properties(
            mqtt_properties.PacketTypes.PUBLISH)
        # If ping had correlation data, echo it back if needed, or just the payload
        client.publish(TOPIC_PONG_SENSOR, msg.payload,
                       qos=0, properties=pong_props)
        print(f"Sent pong to {TOPIC_PONG_SENSOR}")


def on_publish(client, userdata, mid):
    print(f"Message {mid} (QoS > 0) acknowledged by broker.")


# Create MQTT client instance using MQTTv5
publisher_client = mqtt.Client(client_id=CLIENT_ID_PUB, protocol=mqtt.MQTTv5)

# Set username and password
publisher_client.username_pw_set(USERNAME, PASSWORD)

# Configure TLS/SSL
publisher_client.tls_set_context(ssl.create_default_context())

# Set Last Will and Testament on TOPIC_APP_STATUS
lwt_props = mqtt_properties.Properties(mqtt_properties.PacketTypes.WILLMESSAGE)
lwt_props.MessageExpiryInterval = MESSAGE_EXPIRY_INTERVAL_SECONDS  # LWT can also expire
publisher_client.will_set(TOPIC_APP_STATUS, payload=LWT_PAYLOAD,
                          qos=LWT_QOS, retain=True, properties=lwt_props)
print(
    f"LWT configured: Topic='{TOPIC_APP_STATUS}', Payload='{LWT_PAYLOAD}', QoS={LWT_QOS}")

# Assign callback functions
publisher_client.on_connect = on_connect
publisher_client.on_publish = on_publish
publisher_client.on_message = on_message  # Added for commands and pings

# Connect to broker
try:
    publisher_client.connect(BROKER_HOST, BROKER_PORT, 60)
    publisher_client.loop_start()
except Exception as e:
    print(f"Error connecting publisher: {e}")
    exit()

critical_alert_sent = False

try:
    while True:
        moisture_level = random.randint(10, 90)
        payload = str(moisture_level)

        # Publish Moisture Data (QoS 0, Retained, with Expiry)
        pub_props = mqtt_properties.Properties(
            mqtt_properties.PacketTypes.PUBLISH)
        pub_props.MessageExpiryInterval = MESSAGE_EXPIRY_INTERVAL_SECONDS
        result = publisher_client.publish(
            TOPIC_MOISTURE, payload, qos=0, retain=True, properties=pub_props)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(
                f"Published (QoS 0, Retain=True, Expiry={MESSAGE_EXPIRY_INTERVAL_SECONDS}s): {payload}% to {TOPIC_MOISTURE}")
        else:
            print(f"Failed to publish moisture (QoS 0), rc: {result.rc}")

        # Simulate a Critical Alert (QoS 1, with Expiry)
        if moisture_level < 20:
            alert_payload = json.dumps(
                {"level": moisture_level, "message": "Soil very dry! Needs watering.", "timestamp": time.time()})
            alert_props = mqtt_properties.Properties(
                mqtt_properties.PacketTypes.PUBLISH)
            alert_props.MessageExpiryInterval = MESSAGE_EXPIRY_INTERVAL_SECONDS
            result = publisher_client.publish(
                TOPIC_ALERT, alert_payload, qos=1, properties=alert_props)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print(
                    f"Published Alert (QoS 1, Expiry={MESSAGE_EXPIRY_INTERVAL_SECONDS}s): {alert_payload} to {TOPIC_ALERT}")
            else:
                print(f"Failed to publish alert (QoS 1), rc: {result.rc}")

        # Demonstrate QoS 2 (Exactly Once) - e.g., a critical config ack (with Expiry)
        if moisture_level > 85 and not critical_alert_sent:
            config_ack_payload = json.dumps(
                {"sensor_id": CLIENT_ID_PUB, "ack_event": "high_moisture_threshold_confirmed", "timestamp": time.time()})
            config_props = mqtt_properties.Properties(
                mqtt_properties.PacketTypes.PUBLISH)
            config_props.MessageExpiryInterval = MESSAGE_EXPIRY_INTERVAL_SECONDS
            result = publisher_client.publish(
                TOPIC_STATUS, config_ack_payload, qos=2, properties=config_props)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print(
                    f"Published Config Ack (QoS 2, Expiry={MESSAGE_EXPIRY_INTERVAL_SECONDS}s): {config_ack_payload} to {TOPIC_STATUS}")
                critical_alert_sent = True
            else:
                print(f"Failed to publish config ack (QoS 2), rc: {result.rc}")

        time.sleep(10)  # Increased sleep to reduce console noise

except KeyboardInterrupt:
    print("Publisher stopping...")
    # Publish a graceful "offline" message to TOPIC_APP_STATUS for the web app
    offline_payload = json.dumps(
        {"status": "offline_graceful", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})
    # No expiry for graceful offline, it's an immediate, final state. Retain=False.
    publisher_client.publish(
        TOPIC_APP_STATUS, offline_payload, qos=1, retain=False)
    print(f"Published graceful offline status to {TOPIC_APP_STATUS}")
    time.sleep(1)  # Give time for message to send
finally:
    publisher_client.loop_stop()
    publisher_client.disconnect()
    print("Publisher disconnected.")
