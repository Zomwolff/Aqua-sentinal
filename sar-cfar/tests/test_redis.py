import asyncio
import redis.asyncio as aioredis
import os

async def main():
    r = aioredis.from_url("redis://localhost:6380", decode_responses=True)
    try:
        # Read all messages from beginning
        messages = await r.xread({"sar.tasking.events": "0-0"}, count=1000)
        for stream, msgs in messages:
            print(f"Stream: {stream}")
            for msg_id, data in msgs:
                print(f"  {msg_id}: {data}")
    except Exception as e:
        print(f"Error: {e}")
        
    await r.close()

asyncio.run(main())
