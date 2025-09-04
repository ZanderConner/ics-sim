import asyncio, logging, os, math, random, struct, time
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext, ModbusSequentialDataBlock
from pymodbus.server.async_io import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("plant-sim")

# ---- Network / Unit ----
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5020"))
UNIT = int(os.getenv("UNIT_ID", "1"))

# ---- Addresses (all 0-based, zero_mode=True) ---------------------------------
# Coils (FC1/5)
CO_PUMP_CMD      = 0    # write 1=run pump (adds inflow)
CO_HEATER_CMD    = 1    # write 1=heater enabled (drives temp up)
CO_MANUAL_MODE   = 2    # write 1=manual (uses setpoints directly)
CO_FAULT_RESET   = 3    # write 1=clear faults

# Discrete Inputs (FC2)
DI_PUMP_RUNNING  = 0
DI_HEATER_ON     = 1
DI_HIGH_LEVEL    = 2
DI_HIGH_TEMP     = 3

# Holding Registers - READ (FC3)
HR_LEVEL_CM      = 1000   # 0..1000 cm (UINT)
HR_FLOW_IN       = 1001   # L/s (UINT)
HR_FLOW_OUT      = 1002   # L/s (UINT)
HR_TEMP_x10      = 1003   # 0.1°C units (UINT)
HR_PRESSURE_KPA  = 1004   # kPa (UINT)
HR_STATUS_WORD   = 1005   # bitfield: b0=manual b1=fault b2=sensor_fail

# Holding Registers - WRITE (FC6/16)
HR_SP_INFLOW     = 1100   # 0..100 L/s
HR_SP_VALVE_PCT  = 1101   # 0..100 %
HR_SP_TEMP_x10   = 1102   # 0.1°C units
HR_CFG_NOISE     = 1103   # 0/1 noise enable
HR_FAULT_MASK    = 1104   # bit0=freeze level, bit1=spike temp, bit2=offset pressure

# ---- Plant model params -------------------------------------------------------
DT = 0.5                # s, simulation step
LEVEL_MAX = 1000.0      # cm
INFLOW_MAX = 120.0      # L/s if pump+setpoint
OUTFLOW_MAX = 120.0     # L/s through valve at 100%
VALVE_GAIN = 1.0
LEAK_LPS = 0.2
AMBIENT_C = 22.0
HEAT_GAIN = 0.6         # °C/s when heater on (at setpoint)
COOL_TC_S = 120.0       # time constant toward ambient when heater off
PRESSURE_ATM = 101.3    # kPa baseline

def make_ctx() -> ModbusServerContext:
    slave = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0]*16),
        co=ModbusSequentialDataBlock(0, [0]*16),
        ir=ModbusSequentialDataBlock(0, [0]*16),
        hr=ModbusSequentialDataBlock(0, [0]*15000),  # plenty of HR space
        zero_mode=True,
    )
    return ModbusServerContext(slaves={UNIT: slave}, single=False)

def clamp(x, lo, hi): return lo if x < lo else hi if x > hi else x

def hr_get(ctx, addr, count=1):
    return ctx[UNIT].getValues(3, addr, count)

def hr_set(ctx, addr, values):
    ctx[UNIT].setValues(3, addr, list(values))

def co_get(ctx, addr, count=1):
    return ctx[UNIT].getValues(1, addr, count)

def co_set(ctx, addr, values):
    ctx[UNIT].setValues(1, addr, list(values))

def di_set(ctx, addr, values):
    ctx[UNIT].setValues(2, addr, list(values))

def u(val): return int(round(val))

async def plant_loop(ctx: ModbusServerContext):
    # Initial conditions & defaults
    level_cm   = 400.0
    temp_c     = 30.0
    pressure_kpa = PRESSURE_ATM
    inflow_sp  = 20.0
    valve_pct  = 30.0
    temp_sp_c  = 50.0
    noise_on   = 1
    fault_mask = 0
    fault_active = 0
    sensor_fail = 0

    # seed writable registers
    hr_set(ctx, HR_SP_INFLOW,     [u(inflow_sp)])
    hr_set(ctx, HR_SP_VALVE_PCT,  [u(valve_pct)])
    hr_set(ctx, HR_SP_TEMP_x10,   [u(temp_sp_c*10)])
    hr_set(ctx, HR_CFG_NOISE,     [noise_on])
    hr_set(ctx, HR_FAULT_MASK,    [fault_mask])

    last_pub = 0.0

    while True:
        t0 = time.time()

        # Read operator/mode commands
        pump_cmd, heater_cmd, manual_mode, fault_reset = [int(b) for b in co_get(ctx, CO_PUMP_CMD, 4)]
        if fault_reset:
            fault_mask = 0
            fault_active = 0
            sensor_fail = 0
            hr_set(ctx, HR_FAULT_MASK, [0])
            co_set(ctx, CO_FAULT_RESET, [0])

        # Read setpoints/config
        inflow_sp   = hr_get(ctx, HR_SP_INFLOW)[0]
        valve_pct   = clamp(hr_get(ctx, HR_SP_VALVE_PCT)[0], 0, 100)
        temp_sp_c   = hr_get(ctx, HR_SP_TEMP_x10)[0] / 10.0
        noise_on    = 1 if hr_get(ctx, HR_CFG_NOISE)[0] else 0
        fault_mask  = hr_get(ctx, HR_FAULT_MASK)[0]

        # Manual/automatic behavior
        if manual_mode:
            effective_inflow = clamp(inflow_sp if pump_cmd else 0, 0, INFLOW_MAX)
            heater_on = 1 if heater_cmd else 0
            valve_eff = valve_pct / 100.0
        else:
            # simple “auto”: pump on keeps inflow at sp; heater drives toward temp_sp; valve controls outflow
            effective_inflow = clamp(inflow_sp if pump_cmd else 0, 0, INFLOW_MAX)
            heater_on = 1 if (heater_cmd and temp_c < temp_sp_c + 0.3) else 0
            valve_eff = valve_pct / 100.0

        # Outflow depends on valve and (weakly) on level head
        outflow = clamp(OUTFLOW_MAX * valve_eff * math.sqrt(max(level_cm, 1.0) / LEVEL_MAX), 0, OUTFLOW_MAX)

        # Tank level dynamics (cm): assume 1 cm == 1 “unit” volume tick for simplicity
        dlevel = (effective_inflow - outflow - LEAK_LPS) * DT * 0.08  # scale factor to look nice
        if fault_mask & 0x1:   # freeze level (stuck sensor)
            dlevel = 0.0
            sensor_fail = 1
            fault_active = 1
        level_cm = clamp(level_cm + dlevel, 0.0, LEVEL_MAX)

        # Temperature dynamics
        if heater_on:
            # drive toward setpoint with first-order approach
            temp_c += (temp_sp_c - temp_c) * min(1.0, DT * 0.05) + HEAT_GAIN * DT
        else:
            # cool toward ambient
            temp_c += (AMBIENT_C - temp_c) * (DT / max(COOL_TC_S, 1.0))
        if fault_mask & 0x2:   # temperature spikes
            temp_c += random.uniform(2.0, 8.0)
            fault_active = 1

        # Pressure follows level + optional offset fault
        pressure_kpa = PRESSURE_ATM + 0.02 * level_cm
        if fault_mask & 0x4:
            pressure_kpa += 10.0
            fault_active = 1

        # Noise
        if noise_on:
            level_cm     = clamp(level_cm     + random.uniform(-0.6, 0.6), 0, LEVEL_MAX)
            temp_c       = clamp(temp_c       + random.uniform(-0.2, 0.2), -40, 150)
            pressure_kpa = clamp(pressure_kpa + random.uniform(-0.3, 0.3), 60, 300)

        # Alarms & status
        high_level = 1 if level_cm > 900.0 else 0
        high_temp  = 1 if temp_c   > 80.0  else 0
        di_set(ctx, DI_PUMP_RUNNING, [1 if pump_cmd else 0])
        di_set(ctx, DI_HEATER_ON,    [1 if heater_on else 0])
        di_set(ctx, DI_HIGH_LEVEL,   [high_level])
        di_set(ctx, DI_HIGH_TEMP,    [high_temp])

        status_word = (1 if manual_mode else 0) \
                    | ((1 if fault_active else 0) << 1) \
                    | ((1 if sensor_fail else 0) << 2)

        # Publish telemetry
        hr_set(ctx, HR_LEVEL_CM,     [u(level_cm)])
        hr_set(ctx, HR_FLOW_IN,      [u(effective_inflow)])
        hr_set(ctx, HR_FLOW_OUT,     [u(outflow)])
        hr_set(ctx, HR_TEMP_x10,     [u(temp_c * 10.0)])
        hr_set(ctx, HR_PRESSURE_KPA, [u(pressure_kpa)])
        hr_set(ctx, HR_STATUS_WORD,  [status_word])

        # Occasional log line (1/sec)
        now = time.time()
        if now - last_pub > 1.0:
            last_pub = now
            log.info("Lvl=%4.0f cm  Qin=%3.0f L/s  Qout=%3.0f L/s  T=%5.1f C  P=%6.1f kPa  status=0x%04X",
                     level_cm, effective_inflow, outflow, temp_c, pressure_kpa, status_word)

        # loop pacing
        elapsed = time.time() - t0
        await asyncio.sleep(max(0.05, DT - elapsed))

async def main():
    ctx = make_ctx()
    log.info("Starting Modbus TCP plant sim on %s:%d (Unit %d)", HOST, PORT, UNIT)
    await asyncio.gather(
        StartAsyncTcpServer(context=ctx, address=(HOST, PORT)),
        plant_loop(ctx)
    )

if __name__ == "__main__":
    asyncio.run(main())
