"""Фальшивый бот с включёнными темами BotFather."""
from rich_setup import app
import json,sys,urllib.request
request=urllib.request.Request('http://127.0.0.1:'+sys.argv[2]+'/fake/tg/features',data=json.dumps({'topics':True}).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request) as response:response.read()
