import asyncio, logging, os, random, struct
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext, ModbusSequentialDataBlock
from pymodbus.server.async_io import StartAsyncTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("modbus-sim")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5020"))
UNIT = int(os.getenv("UNIT_ID", "1"))
BASE_START = int(os.getenv("BASE_START", "1148"))
ENABLE_FAR = os.getenv("ENABLE_FAR_ADDRS", "true").lower() in ("1","true","yes")
SWAP_WORDS = os.getenv("SWAP_WORDS_FOR_FLOAT", "false").lower() in ("1","true","yes")

def f32_to_words(val: float, swap: bool=False):
    b = struct.pack(">f", val)
    hi = int.from_bytes(b[0:2], "big")
    lo = int.from_bytes(b[2:4], "big")
    return (lo, hi) if swap else (hi, lo)

def make_ctx():
    slave = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0]*1),
        co=ModbusSequentialDataBlock(0, [0]*1),
        ir=ModbusSequentialDataBlock(0, [0]*1),
        hr=ModbusSequentialDataBlock(0, [0]*10000),
        zero_mode=True,
    )
    return ModbusServerContext(slaves={UNIT: slave}, single=False)

async def updater(ctx: ModbusServerContext):
    fc = 3
    while True:
        a = BASE_START
        u16a = random.randint(0, 65535)
        u16b = random.randint(0, 65535)
        f32  = random.uniform(0.0, 100000.0)
        hi, lo = f32_to_words(f32, SWAP_WORDS)
        ctx[UNIT].setValues(fc, a + 0, [u16a])
        ctx[UNIT].setValues(fc, a + 1, [u16b])
        ctx[UNIT].setValues(fc, a + 2, [hi, lo])
        if ENABLE_FAR:
            ctx[UNIT].setValues(fc, 2305, [random.randint(0, 65535)])
            hi2, lo2 = f32_to_words(random.uniform(0.0, 100000.0), SWAP_WORDS)
            ctx[UNIT].setValues(fc, 4188, [hi2, lo2])
        await asyncio.sleep(1.0)

async def main():
    log.info("Starting Modbus TCP server on %s:%s (Unit %d)", HOST, PORT, UNIT)
    ctx = make_ctx()
    server = asyncio.create_task(StartAsyncTcpServer(context=ctx, address=(HOST, PORT)))
    writer = asyncio.create_task(updater(ctx))
    await asyncio.gather(server, writer)

if __name__ == "__main__":
    asyncio.run(main())
