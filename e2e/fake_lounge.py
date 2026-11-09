"""Fake YouTube lounge pairing endpoints for automated UI tests (SIMULATED, not YouTube)."""
from aiohttp import web

SCREENS = {"123456789012": ("FAKESCREEN" + "B" * 54, "Bedroom TV"), "111122223333": ("FAKESCREEN" + "A" * 54, "Living Room TV")}


async def get_screen(request):
    data = await request.post()
    code = data.get("pairing_code", "")
    if code not in SCREENS:
        return web.Response(status=404, text="not found")
    sid, name = SCREENS[code]
    return web.json_response({"screen": {"screenId": sid, "name": name, "loungeToken": "FAKELOUNGETOKEN" + code}})


async def token_batch(request):
    data = await request.post()
    return web.json_response({"screens": [{"screenId": data.get("screen_ids"), "loungeToken": "FAKELOUNGETOKEN"}]})


async def availability(request):
    return web.json_response({"screens": [{"status": "online"}]})


app = web.Application()
app.router.add_post("/pairing/get_screen", get_screen)
app.router.add_post("/pairing/get_lounge_token_batch", token_batch)
app.router.add_post("/pairing/get_screen_availability", availability)
web.run_app(app, host="127.0.0.1", port=8299, print=None)
