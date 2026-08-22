import asyncio
import websockets

async def test():
    try:
        async with websockets.connect("ws://localhost:5173/live") as websocket:
            print("Connected to Vite!")
            msg = await websocket.recv()
            print(f"Received from Vite: {msg}")
    except Exception as e:
        print(f"Vite Error: {e}")
        
    try:
        async with websockets.connect("ws://localhost:8015/live") as websocket:
            print("Connected to api-gateway directly!")
            msg = await websocket.recv()
            print(f"Received from api-gateway: {msg}")
    except Exception as e:
        print(f"api-gateway Error: {e}")

asyncio.run(test())
