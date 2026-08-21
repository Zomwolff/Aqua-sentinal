import asyncio
import websockets
import json

async def connect_ais_stream():
    async with websockets.connect("wss://stream.aisstream.io/v0/stream") as websocket:
        subscribe_message = {"APIKey": "5012e3c36f01f59bd3e2e4c5457d47054f887208", "BoundingBoxes": [[[5.0, 76.0], [10.0, 82.0]]]}
        subscribe_message_json = json.dumps(subscribe_message)
        await websocket.send(subscribe_message_json)
        print("Connected and subscribed!")
        try:
            async with asyncio.timeout(10):
                async for message_json in websocket:
                    message = json.loads(message_json)
                    print(message)
                    break
        except TimeoutError:
            print("Timeout waiting for messages")

asyncio.run(connect_ais_stream())
