#!/usr/bin/env python3
"""
Modbus-TCP plant simulator for OpenPLC/SCADA-LTS training
- Coils (FC1/5/15)       : 0..3   -> PumpCmd, HeaterCmd, ManualMode, Spare
- Discrete Inputs (FC2)  : 0..3   -> HighLevel, HighTemp, Spare, Spare
- Holding READ (FC3)     : 1000..1005 -> Level_cm, Qin_lps, Qout_lps, Temp_x10C, Pressure_kPa, StatusWord
- Holding WRITE (FC16)   : 1100..1104 -> Inflow_SP, ValvePct_SP, Temp_SP_x10C, NoiseEnable, FaultMask

Test (Schneider addressing with modbus-cli):
  Read sensors:    modbus read 192.25.0.9 %MW1000 6 --schneider -s 1 -p 5020
  Pump OFF/ON:     modbus write 192.25.0.9 %M0 0 --schneider -s 1 -p 5020
                   modbus write 192.25.0.9 %M0 1 --schneider -s 1 -p 5020
  Valve 0%/60%:    modbus write 192.25.0.9 %MW1101 0  --schneider -s 1 -p 5020
                   modbus write 192.25.0.9 %MW1101 60 --schneider -s 1 -p 5020
  Temp SP 75.0C:   modbus write 192.25.0.9 %MW1102 750 --schneider -s 1 -p 5020
  Heater on: modbus write -p 5021 -s 1 --schneider 192.25.0.9 %M1 1
"""

import asyncio
import logging
import random
import time
from pymodbus.datastore import (
    ModbusSlaveContext, ModbusServerContext, ModbusSequentialDataBlock
)
from pymodbus.server import StartAsyncTcpServer

# --------------------------
# Config
# --------------------------
HOST = "0.0.0.0"
PORT = 5020
UNIT_ID = 1

SCAN_SEC = 1.0        # physics update period
AMBIENT_C = 22.0
TANK_MAX_CM = 1000.0
MAX_QOUT_LPS = 80.0   # max outflow when valve=100%
LEVEL_PER_FLOW = 0.5  # cm per (L/s * s), super-simplified

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("modbus-sim")

# --------------------------
# Datastore helpers
# --------------------------
def mk_block(size, init=0):
    return ModbusSequentialDataBlock(0, [init] * size)

# Build one slave with generous blocks; zero_mode=True uses 0-based addressing
di_block = mk_block(64, 0)
co_block = mk_block(64, 0)
hr_block = mk_block(20000, 0)  # plenty to cover 1000..1104
ir_block = mk_block(64, 0)     # not used (FC4)

slave = ModbusSlaveContext(di=di_block, co=co_block, hr=hr_block, ir=ir_block, zero_mode=True)
context = ModbusServerContext(slaves={UNIT_ID: slave}, single=False)

def HR(addr, count=1):
    return context[UNIT_ID].getValues(3, addr, count)

def WR_HR(addr, values):
    context[UNIT_ID].setValues(3, addr, values)

def COIL(addr, count=1):
    return context[UNIT_ID].getValues(1, addr, count)

def WR_COIL(addr, value):
    context[UNIT_ID].setValues(1, addr, [1 if value else 0])

def WR_DI(addr, values):
    context[UNIT_ID].setValues(2, addr, values)

# --------------------------
# Initial state
# --------------------------
def seed_initial():
    # Commands (writes from PLC/SCADA)
    # inflow_sp=60, valve=50%, temp_sp=50.0C, noise=1, fault=0
    WR_HR(1100, [60, 50, 500, 1, 0])

    # Coils: pump ON, heater OFF, manual OFF
    WR_COIL(0, True)   # PumpCmd
    WR_COIL(1, False)  # HeaterCmd
    WR_COIL(2, False)  # ManualMode
    WR_COIL(3, False)  # Spare

    # Sensors: level=600cm, Qin=60, Qout=30, Temp=50.0C, Pressure=120kPa, Status=0
    WR_HR(1000, [600, 60, 30, 500, 120, 0])

# --------------------------
# Physics loop
# --------------------------
async def physics_loop():
    log.info(f"Starting Modbus TCP slave on {HOST}:{PORT} (Unit {UNIT_ID})")
    seed_initial()
    last = time.time()

    while True:
        now = time.time()
        dt = max(0.1, min(5.0, now - last))
        last = now

        # Read commands
        inflow_sp, valve_sp, temp_sp_x10, noise_en, faultmask = HR(1100, 5)
        pump_on  = bool(COIL(0, 1)[0])
        heater_on= bool(COIL(1, 1)[0])
        manual   = bool(COIL(2, 1)[0])

        temp_sp = temp_sp_x10 / 10.0

        # Read current sensors
        level_cm, qin_lps, qout_lps, temp_x10, press_kpa, status = HR(1000, 6)
        temp_c = temp_x10 / 10.0

        # Apply faults/overrides
        valve_pct = max(0.0, min(100.0, float(valve_sp)))
        if faultmask & 0x0001:  # bit 0: force valve closed
            valve_pct = 0.0

        # Control surfaces
        q_in  = float(inflow_sp) if pump_on else 0.0
        q_out = (valve_pct / 100.0) * MAX_QOUT_LPS

        # Manual mode (optional): if manual, let valve directly drive outflow regardless of level logic
        # (already the case above—no extra logic needed for this simple sim)

        # Integrate level (bounded)
        level_f = float(level_cm) + LEVEL_PER_FLOW * (q_in - q_out) * dt
        level_f = max(0.0, min(TANK_MAX_CM, level_f))

        # Temperature dynamics
        if heater_on:
            # move toward setpoint
            temp_c = temp_c + 0.05 * (temp_sp - temp_c) * dt
        else:
            # cool toward ambient
            temp_c = temp_c - 0.005 * (temp_c - AMBIENT_C) * dt

        # Pressure ~ head
        press_kpa = int(max(0, min(65535, round(level_f * 0.2))))

        # Status bits
        status = 0
        if level_f >= 900.0:
            status |= 0x0001  # high-high level
        if temp_c >= temp_sp + 5.0:
            status |= 0x0002  # high temp over setpoint

        # Noise injection
        if noise_en:
            level_f += random.uniform(-0.5, 0.5)
            temp_c  += random.uniform(-0.1, 0.1)
            q_in    += random.uniform(-0.5, 0.5)
            q_out   += random.uniform(-0.5, 0.5)
            level_f = max(0.0, min(TANK_MAX_CM, level_f))

        # Discrete inputs (alarms)
        di_hl = 1 if level_f >= 800.0 else 0
        di_ht = 1 if temp_c >= (temp_sp + 3.0) else 0
        WR_DI(0, [di_hl, di_ht, 0, 0])

        # Write back sensors
        WR_HR(1000, [
            int(max(0, min(65535, round(level_f)))),
            int(max(0, min(65535, round(q_in)))),
            int(max(0, min(65535, round(q_out)))),
            int(max(0, min(65535, round(temp_c * 10)))),
            press_kpa,
            status
        ])

        # Logs: show both commands and resulting sensors
        log.info(
            "Cmds {Pump:%s Heater:%s Manual:%s | Qin_SP:%d lps Valve_SP:%d%% Temp_SP:%.1fC Noise:%d Fault:0x%04X}  "
            "=> Sensors {Level:%d cm Qin:%d lps Qout:%d lps Temp:%.1f C Press:%d kPa Status:0x%04X}",
            int(pump_on), int(heater_on), int(manual),
            int(inflow_sp), int(valve_sp), temp_sp, int(noise_en), int(faultmask),
            int(HR(1000,1)[0]), int(HR(1001,1)[0]), int(HR(1002,1)[0]),
            HR(1003,1)[0]/10.0, int(HR(1004,1)[0]), int(HR(1005,1)[0])
        )

        await asyncio.sleep(SCAN_SEC)

# --------------------------
# Main
# --------------------------
async def main():
    # Turn on pymodbus internal logs for client connect/disconnect visibility
    logging.getLogger("pymodbus").setLevel(logging.INFO)
    server_task = asyncio.create_task(StartAsyncTcpServer(context, address=(HOST, PORT)))
    physics_task = asyncio.create_task(physics_loop())
    log.info("Server listening; awaiting clients...")
    await asyncio.gather(server_task, physics_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
