import paho.mqtt.client as mqtt
import ssl
import os
import json
import time
import threading
from flask import Flask, render_template, Response, stream_with_context
from queue import Queue
from collections import deque
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
CLIENT_ID_SUB_WEB = "plant_monitor_dashboard"

# --- Flask App Setup ---
app = Flask(__name__)

# --- Data Storage (thread-safe for SSE) ---
# We'll use a queue to pass messages from MQTT thread to Flask SSE handler
message_queue = Queue()
MAX_ALERTS = 5
alerts_history = deque(maxlen=MAX_ALERTS)
current_moisture = "N/A"
sensor_status = {"status": "Unknown", "timestamp": time.time()}


# --- MQTT Client Functions ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT: Subscriber connected to Broker.")
        client.subscribe([(TOPIC_MOISTURE, 0), (TOPIC_STATUS, 1), (TOPIC_ALERT, 1)])
        print(f"MQTT: Subscribed to {TOPIC_MOISTURE}, {TOPIC_STATUS}, {TOPIC_ALERT}")
        # Send initial state to queue for any connected web clients
        message_queue.put(json.dumps({"type": "moisture", "value": current_moisture, "retain": True}))
        message_queue.put(json.dumps({"type": "status", "data": sensor_status, "retain": True}))
        for alert in list(alerts_history): # Send existing alerts
             message_queue.put(json.dumps({"type": "alert", "data": alert, "retain": True}))
    else:
        print(f"MQTT: Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    global current_moisture, sensor_status # Allow modification of globals
    payload_str = msg.payload.decode()
    print(f"MQTT: Received '{payload_str}' on topic '{msg.topic}' (Retained: {msg.retain})")

    data_to_send = None
    if msg.topic == TOPIC_MOISTURE:
        current_moisture = payload_str
        data_to_send = {"type": "moisture", "value": current_moisture, "retain": msg.retain}
    elif msg.topic == TOPIC_STATUS:
        try:
            status_data = json.loads(payload_str)
            sensor_status = status_data # Update global status
            data_to_send = {"type": "status", "data": status_data, "retain": msg.retain}
        except json.JSONDecodeError:
            print(f"MQTT: Error decoding status JSON: {payload_str}")
            data_to_send = {"type": "status", "data": {"status": "Error decoding", "raw": payload_str}, "retain": msg.retain}
    elif msg.topic == TOPIC_ALERT:
        try:
            alert_data = json.loads(payload_str)
            alerts_history.append(alert_data) # Add to history
            data_to_send = {"type": "alert", "data": alert_data, "retain": msg.retain}
        except json.JSONDecodeError:
            print(f"MQTT: Error decoding alert JSON: {payload_str}")
            data_to_send = {"type": "alert", "data": {"message": "Error decoding alert", "raw": payload_str}, "retain": msg.retain}

    if data_to_send:
        message_queue.put(json.dumps(data_to_send))


def mqtt_thread_worker():
    mqtt_client = mqtt.Client(client_id=CLIENT_ID_SUB_WEB)
    mqtt_client.username_pw_set(USERNAME, PASSWORD)
    mqtt_client.tls_set_context(ssl.create_default_context())
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    while True: # Keep trying to connect
        try:
            print("MQTT: Attempting to connect to broker...")
            mqtt_client.connect(BROKER_HOST, BROKER_PORT, 60)
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
                           initial_status=sensor_status,
                           initial_alerts=list(alerts_history))

@app.route('/events')
def events():
    def generate_sse_events():
        # Send current state immediately upon connection
        # This ensures the page has data if it loads after MQTT messages have already arrived
        # Note: This might send data that was also sent by on_connect if the client connects fast enough
        # but the JS side can be made idempotent.
        initial_data = [
            {"type": "moisture", "value": current_moisture, "retain": True},
            {"type": "status", "data": sensor_status, "retain": True}
        ]
        for alert in list(alerts_history):
            initial_data.append({"type": "alert", "data": alert, "retain": True})

        for item in initial_data:
            yield f"data: {json.dumps(item)}\n\n"
            time.sleep(0.01) # Small delay to ensure messages are processed separately by client

        # Then, listen for new messages from the queue
        while True:
            try:
                message_json = message_queue.get(timeout=60) # Wait for a message
                yield f"data: {message_json}\n\n"
                message_queue.task_done()
            except Exception: # Timeout or other queue error
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
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True, use_reloader=False)
    # use_reloader=False is important when running threads like MQTT client,
    # otherwise Flask might start it twice.