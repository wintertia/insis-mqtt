import os
import time
import json
import ssl
import threading
import paho.mqtt.client as mqtt
from flask import Flask, render_template, Response, stream_with_context
from queue import Queue
from collections import deque
from dotenv import load_dotenv
from paho.mqtt import properties as mqtt_properties

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
# For general status, like config acks from sensor
TOPIC_STATUS = "office/plant/status"
TOPIC_ALERT = "office/plant/alert"
CLIENT_ID_SUB_WEB = "plant_monitor_dashboard"

# New Topic for Web App's primary sensor status (online/offline/lwt)
TOPIC_APP_STATUS = "office/plant/app_status"

# New Topics for Request/Response and Ping/Pong
TOPIC_COMMAND_REQUEST = "office/plant/command/request"
TOPIC_COMMAND_RESPONSE = "office/plant/command/response"
TOPIC_PING_SENSOR = "office/plant/ping/sensor"  # Subscriber pings sensor
TOPIC_PONG_SENSOR = "office/plant/pong/sensor"  # Sensor pongs subscriber

# --- Flask App Setup ---
app = Flask(__name__)

# --- Data Storage (thread-safe for SSE) ---
# We'll use a queue to pass messages from MQTT thread to Flask SSE handler
message_queue = Queue()
MAX_ALERTS = 5
alerts_history = deque(maxlen=MAX_ALERTS)
current_moisture = "N/A"
# sensor_status will now be updated by messages on TOPIC_APP_STATUS
sensor_app_status = {"status": "Unknown", "timestamp": time.time()}
ping_pong_stats = {"last_ping_sent": None,
                   "last_pong_received": None, "latency_ms": None}

# For tracking request/response
active_requests = {}  # Store correlation_id: timestamp

# --- MQTT Client Functions ---


def on_connect(client, userdata, flags, rc, properties=None):  # Added properties for MQTTv5
    if rc == 0:
        print("MQTT: Subscriber connected to Broker.")
        client.subscribe([
            (TOPIC_MOISTURE, 0),
            (TOPIC_APP_STATUS, 1),  # Subscribe to the new app status topic
            # Still subscribe to general status for other messages if any
            (TOPIC_STATUS, 1),
            (TOPIC_ALERT, 1),
            (TOPIC_COMMAND_RESPONSE, 1),
            (TOPIC_PONG_SENSOR, 0)
        ])
        print(
            f"MQTT: Subscribed to {TOPIC_MOISTURE}, {TOPIC_APP_STATUS}, {TOPIC_STATUS}, {TOPIC_ALERT}, {TOPIC_COMMAND_RESPONSE}, {TOPIC_PONG_SENSOR}")
        # Send initial state to queue for any connected web clients
        message_queue.put(json.dumps(
            {"type": "moisture", "value": current_moisture, "retain": True}))
        message_queue.put(json.dumps(
            {"type": "app_status", "data": sensor_app_status, "retain": True}))
        for alert in list(alerts_history):  # Send existing alerts
            message_queue.put(json.dumps(
                {"type": "alert", "data": alert, "retain": True}))
    else:
        print(f"MQTT: Failed to connect, return code {rc}")


def on_message(client, userdata, msg):
    # Allow modification of globals
    global current_moisture, sensor_app_status, ping_pong_stats
    payload_str = msg.payload.decode()
    print(
        f"MQTT: Received '{payload_str}' on topic '{msg.topic}' (Retained: {msg.retain})")

    data_to_send = None
    if msg.topic == TOPIC_MOISTURE:
        current_moisture = payload_str
        data_to_send = {"type": "moisture",
                        "value": current_moisture, "retain": msg.retain}

    elif msg.topic == TOPIC_APP_STATUS:  # Handle the new app status topic
        try:
            status_data = json.loads(payload_str)
            sensor_app_status = status_data  # Update global app status
            data_to_send = {"type": "app_status",
                            "data": status_data, "retain": msg.retain}
        except json.JSONDecodeError:
            print(f"MQTT: Error decoding app_status JSON: {payload_str}")
            data_to_send = {"type": "app_status", "data": {
                "status": "Error decoding", "raw": payload_str}, "retain": msg.retain}

    # General status messages (e.g. config acks from sensor)
    elif msg.topic == TOPIC_STATUS:
        try:
            status_data = json.loads(payload_str)
            # This could be used for other status updates not directly for the main sensor online/offline state
            # For now, we can just log it or send it as a generic status update to UI if needed
            print(f"MQTT: Received general status: {status_data}")
            # Example: forward as a generic event or handle specific known statuses
            data_to_send = {"type": "generic_status",
                            "data": status_data, "retain": msg.retain}
        except json.JSONDecodeError:
            print(f"MQTT: Error decoding status JSON: {payload_str}")
            data_to_send = {"type": "generic_status", "data": {
                "status": "Error decoding", "raw": payload_str}, "retain": msg.retain}

    elif msg.topic == TOPIC_ALERT:
        try:
            alert_data = json.loads(payload_str)
            # Add server-side timestamp if not present, for consistency
            if 'timestamp' not in alert_data:
                alert_data['timestamp'] = time.time()
            alerts_history.append(alert_data)
            data_to_send = {"type": "alert",
                            "data": alert_data, "retain": msg.retain}
        except json.JSONDecodeError:
            print(f"MQTT: Error decoding alert JSON: {payload_str}")
            data_to_send = {"type": "alert", "data": {"message": "Error decoding alert",
                                                      "raw": payload_str, "timestamp": time.time()}, "retain": msg.retain}

    elif msg.topic == TOPIC_COMMAND_RESPONSE:
        correlation_id = None
        if msg.properties and msg.properties.CorrelationData:
            correlation_id = msg.properties.CorrelationData.decode()

        print(f"Received command response. Correlation ID: {correlation_id}")
        if correlation_id and correlation_id in active_requests:
            request_time = active_requests.pop(correlation_id)
            latency = (time.time() - request_time) * 1000
            print(
                f"Command response for {correlation_id} received in {latency:.2f} ms.")
            # You could forward this to the web client if a specific UI element initiated it
            # For now, just logging and potentially updating a generic "last command response" field
            data_to_send = {"type": "command_response", "data": json.loads(
                payload_str), "correlation_id": correlation_id, "latency_ms": latency}
        else:
            # Unsolicited or late response
            data_to_send = {"type": "command_response", "data": json.loads(
                payload_str), "correlation_id": "unknown"}

    elif msg.topic == TOPIC_PONG_SENSOR:
        ping_pong_stats["last_pong_received"] = time.time()
        # Assuming pong payload is the original ping timestamp (sent by sensor)
        try:
            # The sensor should echo the timestamp sent in ping
            ping_timestamp = float(payload_str)
            latency = (
                ping_pong_stats["last_pong_received"] - ping_timestamp) * 1000
            ping_pong_stats["latency_ms"] = round(latency, 2)
            print(
                f"MQTT: Pong received. Latency: {ping_pong_stats['latency_ms']:.2f} ms")
        except ValueError:
            print(f"MQTT: Pong payload not a valid timestamp: {payload_str}")
            ping_pong_stats["latency_ms"] = "Error"

        data_to_send = {"type": "pong", "stats": ping_pong_stats}

    if data_to_send:
        message_queue.put(json.dumps(data_to_send))


def send_ping_to_sensor(client):
    ping_timestamp = time.time()
    ping_pong_stats["last_ping_sent"] = ping_timestamp
    # The payload of the ping is the current timestamp, which the sensor should echo back in the pong.
    client.publish(TOPIC_PING_SENSOR, payload=str(ping_timestamp), qos=0)
    print(
        f"MQTT: Sent PING to {TOPIC_PING_SENSOR} with timestamp {ping_timestamp}")
    # Update UI immediately that ping was sent
    message_queue.put(json.dumps(
        {"type": "ping_sent", "timestamp": ping_timestamp}))


def request_sensor_details(client):
    correlation_id = f"req_{int(time.time() * 1000)}"  # Unique ID
    active_requests[correlation_id] = time.time()

    props = mqtt_properties.Properties(mqtt_properties.PacketTypes.PUBLISH)
    props.ResponseTopic = TOPIC_COMMAND_RESPONSE
    props.CorrelationData = correlation_id.encode()

    command_payload = json.dumps({"command": "get_details"})
    client.publish(TOPIC_COMMAND_REQUEST, command_payload,
                   qos=1, properties=props)
    print(
        f"Sent command '{command_payload}' to {TOPIC_COMMAND_REQUEST} with CID {correlation_id}")
    message_queue.put(json.dumps(
        {"type": "command_sent", "command": "get_details", "correlation_id": correlation_id}))


def mqtt_thread_worker():
    # Use MQTTv5
    mqtt_client = mqtt.Client(
        client_id=CLIENT_ID_SUB_WEB, protocol=mqtt.MQTTv5)
    mqtt_client.username_pw_set(USERNAME, PASSWORD)
    mqtt_client.tls_set_context(ssl.create_default_context())
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    while True:  # Keep trying to connect
        try:
            print("MQTT: Attempting to connect to broker...")
            mqtt_client.connect(BROKER_HOST, BROKER_PORT, 60)

            # Start a periodic ping after connection
            # And an initial request for sensor details
            def periodic_tasks():
                if mqtt_client.is_connected():
                    send_ping_to_sensor(mqtt_client)
                    # Optionally, request details periodically or on demand
                    # request_sensor_details(mqtt_client)
                # Ping every 30 seconds
                threading.Timer(30, periodic_tasks).start()

            # Send initial ping and request details once connected
            # Delay slightly to ensure subscriptions are processed by broker
            threading.Timer(
                2, lambda: send_ping_to_sensor(mqtt_client)).start()
            threading.Timer(
                3, lambda: request_sensor_details(mqtt_client)).start()
            # Start periodic pings
            # threading.Timer(30, periodic_tasks).start() # Start after initial tasks

            mqtt_client.loop_forever()  # Blocks until disconnect
        except ConnectionRefusedError:
            print("MQTT: Connection refused. Retrying in 10 seconds...")
        except Exception as e:
            print(f"MQTT: Connection error: {e}. Retrying in 10 seconds...")

        # If loop_forever() exits (e.g., due to disconnect), wait before retrying
        print("MQTT: Disconnected. Will attempt to reconnect...")
        time.sleep(10)


# --- Flask Routes ---
@app.route('/')
def index():
    # Pass current state for initial render (though SSE will update it)
    return render_template('index.html',
                           initial_moisture=current_moisture,
                           initial_app_status=sensor_app_status,  # Use new status variable
                           initial_alerts=list(alerts_history),
                           initial_ping_stats=ping_pong_stats)


@app.route('/events')
def events():
    def generate_sse_events():
        # Send current state immediately upon connection
        initial_data = [
            {"type": "moisture", "value": current_moisture, "retain": True},
            {"type": "app_status", "data": sensor_app_status,
                "retain": True},  # Use new status type
            {"type": "pong", "stats": ping_pong_stats}
        ]
        for alert in list(alerts_history):
            initial_data.append(
                {"type": "alert", "data": alert, "retain": True})

        for item in initial_data:
            yield f"data: {json.dumps(item)}\n\n"
            # Small delay to ensure messages are processed separately by client
            time.sleep(0.01)

        # Then, listen for new messages from the queue
        while True:
            try:
                message_json = message_queue.get(
                    timeout=60)  # Wait for a message
                yield f"data: {message_json}\n\n"
                message_queue.task_done()
            except Exception:  # Timeout or other queue error
                # Send a keep-alive comment to prevent connection closure by proxies or browser
                yield ": keep-alive\n\n"

    # Using stream_with_context to ensure the request context is available if needed
    return Response(stream_with_context(generate_sse_events()), mimetype='text/event-stream')


if __name__ == '__main__':
    # Start MQTT client in a separate thread
    mqtt_thread = threading.Thread(target=mqtt_thread_worker, daemon=True)
    mqtt_thread.start()

    # Run Flask app (debug=False for production, use a proper WSGI server)
    # Use host='0.0.0.0' to make it accessible on your network
    app.run(host='0.0.0.0', port=5000, debug=True,
            threaded=True, use_reloader=False)
    # use_reloader=False is important when running threads like MQTT client,
    # otherwise Flask might start it twice.
