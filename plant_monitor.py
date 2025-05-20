import paho.mqtt.client as mqtt
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
TOPIC_STATUS = "office/plant/status"
TOPIC_ALERT = "office/plant/alert"
CLIENT_ID_PUB = "plant_monitor_sensor"

# --- Last Will and Testament ---
LWT_PAYLOAD = json.dumps({"status": "offline", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})
LWT_QOS = 1 # LWT message should be delivered reliably

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Publisher connected to MQTT Broker (rc: {rc})")
        # Publish an "online" status message (not retained, QoS 1)
        online_payload = json.dumps({"status": "online", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})
        client.publish(TOPIC_STATUS, online_payload, qos=1)
        print(f"Published online status to {TOPIC_STATUS}")
    else:
        print(f"Publisher failed to connect, return code {rc}\n")

def on_publish(client, userdata, mid):
    # This callback is triggered when a message with QoS > 0 is acknowledged
    print(f"Message {mid} (QoS > 0) acknowledged by broker.")

# Create MQTT client instance
publisher_client = mqtt.Client(client_id=CLIENT_ID_PUB)

# Set username and password
publisher_client.username_pw_set(USERNAME, PASSWORD)

# Configure TLS/SSL for MQTTS
publisher_client.tls_set_context(ssl.create_default_context())

# --- Set Last Will and Testament ---
# This message will be sent by the broker if the client disconnects ungracefully.
# The LWT message itself should also have a QoS.
publisher_client.will_set(TOPIC_STATUS, payload=LWT_PAYLOAD, qos=LWT_QOS, retain=True) # LWT can also be retained
print(f"LWT configured: Topic='{TOPIC_STATUS}', Payload='{LWT_PAYLOAD}', QoS={LWT_QOS}")


# Assign callback functions
publisher_client.on_connect = on_connect
publisher_client.on_publish = on_publish # Important for QoS 1 & 2

# Connect to broker
try:
    publisher_client.connect(BROKER_HOST, BROKER_PORT, 60)
    publisher_client.loop_start()
except Exception as e:
    print(f"Error connecting publisher: {e}")
    exit()

critical_alert_sent = False # To send QoS 2 message once

try:
    while True:
        moisture_level = random.randint(10, 90) # Wider range for testing alerts
        payload = str(moisture_level)

        # --- Publish Moisture Data (QoS 0, Retained) ---
        # Retained: New subscribers will get the last value immediately.
        # QoS 0: "Fire and forget" for frequent updates.
        result = publisher_client.publish(TOPIC_MOISTURE, payload, qos=0, retain=True)
        # result.wait_for_publish() # Not strictly needed for QoS 0, but good practice if checking rc
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"Published (QoS 0, Retain=True): {payload}% to {TOPIC_MOISTURE}")
        else:
            print(f"Failed to publish moisture (QoS 0), rc: {result.rc}")


        # --- Simulate a Critical Alert (QoS 1) ---
        if moisture_level < 20:
            alert_payload = json.dumps({"level": moisture_level, "message": "Soil very dry! Needs watering."})
            # QoS 1: At least once delivery. Broker will retry until acknowledged.
            result = publisher_client.publish(TOPIC_ALERT, alert_payload, qos=1)
            # result.wait_for_publish() # Blocks until PUBACK is received
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print(f"Published Alert (QoS 1): {alert_payload} to {TOPIC_ALERT}")
            else:
                print(f"Failed to publish alert (QoS 1), rc: {result.rc}")

        # --- Demonstrate QoS 2 (Exactly Once) - e.g., a critical config ack ---
        # We'll send this only once for demonstration.
        if moisture_level > 85 and not critical_alert_sent:
            config_ack_payload = json.dumps({"sensor_id": CLIENT_ID_PUB, "ack_event": "high_moisture_threshold_confirmed"})
            # QoS 2: Exactly once delivery. Most overhead, ensures no loss or duplication.
            result = publisher_client.publish(TOPIC_STATUS, config_ack_payload, qos=2)
            # result.wait_for_publish() # Blocks until PUBCOMP is received
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print(f"Published Config Ack (QoS 2): {config_ack_payload} to {TOPIC_STATUS}")
                critical_alert_sent = True # Send only once
            else:
                print(f"Failed to publish config ack (QoS 2), rc: {result.rc}")

        # --- Sleep to simulate sensor reading interval ---
        time.sleep(2)

except KeyboardInterrupt:
    print("Publisher stopping...")
    # Publish a graceful "offline" message before disconnecting
    offline_payload = json.dumps({"status": "offline_graceful", "sensor_id": CLIENT_ID_PUB, "timestamp": time.time()})
    publisher_client.publish(TOPIC_STATUS, offline_payload, qos=1, retain=False) # Don't retain graceful shutdown
    time.sleep(1) # Give time for message to send
finally:
    publisher_client.loop_stop()
    publisher_client.disconnect()
    print("Publisher disconnected.")