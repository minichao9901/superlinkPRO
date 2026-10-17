import socket
import sys
import time

HOST = '127.0.0.1'
PORT = 9090
LOG_FILE = 'rtt_log.txt'

def main():
    print(f"Connecting to OpenOCD RTT server at {HOST}:{PORT}...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            print(f"Connected! Logging to {LOG_FILE}...")
            print("Speed monitoring started (update every 1s)...")
            
            with open(LOG_FILE, 'wb') as f:
                total_bytes = 0
                start_time = time.time()
                last_check_time = start_time
                last_bytes = 0

                while True:
                    data = s.recv(4096)  # Increase buffer size for better throughput
                    if not data:
                        break
                    
                    # Write to file
                    f.write(data)
                    f.flush()  # Reduce flush frequency for better performance
                    
                    # Update counters
                    data_len = len(data)
                    total_bytes += data_len
                    
                    # Speed calculation
                    current_time = time.time()
                    if current_time - last_check_time >= 1.0:
                        interval = current_time - last_check_time
                        bytes_in_interval = total_bytes - last_bytes
                        speed_kbps = (bytes_in_interval / 1024) / interval
                        avg_speed_kbps = (total_bytes / 1024) / (current_time - start_time)
                        
                        sys.stdout.write(f"\rSpeed: {speed_kbps:8.2f} KB/s | Avg: {avg_speed_kbps:8.2f} KB/s | Total: {total_bytes/1024:8.2f} KB")
                        sys.stdout.flush()
                        
                        last_check_time = current_time
                        last_bytes = total_bytes

    except ConnectionRefusedError:
        print("\nError: Could not connect to OpenOCD. Make sure OpenOCD is running with RTT server enabled.")
    except KeyboardInterrupt:
        print("\nLogging stopped by user.")
        print(f"Total received: {total_bytes/1024:.2f} KB")
    except Exception as e:
        print(f"\nError: {e}")

if __name__ == "__main__":
    main()
