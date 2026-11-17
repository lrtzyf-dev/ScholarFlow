"""Actual UI server; all supplier calls explicitly synthetic MockTransport."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx

from app.main import app
from app import model_profiles
from app.db import init_db,SessionLocal
from app.llm import APIClient
from app.runtime_context import current_runtime_config
from app.translate import translation_run

CONTROL=Path(os.environ["TRANSLATION_RUN_CONTROL"]);CONTROL.mkdir(parents=True,exist_ok=True)
RECEIPTS=CONTROL/"synthetic-calls.jsonl"
guard=threading.Lock()


def provider(request):
    assert request.url.host=="translation-run.synthetic.test" and request.url.path=="/v1/chat/completions"
    body=json.loads(request.content)
    assert body["model"]=="Translation-UI-Bound-Case"
    with guard:
        count=len(RECEIPTS.read_text().splitlines())+1 if RECEIPTS.exists() else 1
        with RECEIPTS.open("a",encoding="utf-8") as stream:stream.write(json.dumps({"number":count,"model":body["model"],"synthetic":True})+"\n")
    while not (CONTROL/f"release-{count}").exists():time.sleep(.02)
    return httpx.Response(200,json={"id":"synthetic-"+str(count),"choices":[{"index":0,"message":{
        "role":"assistant","content":"Synthetic translation: "+body["messages"][-1]["content"]},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}})


_client=httpx.Client
httpx.Client=lambda *a,**k:_client(*a,transport=httpx.MockTransport(provider),**k)
translation_run.get_llm=lambda:APIClient(runtime_config=current_runtime_config(),synthetic=True)
init_db()
with SessionLocal.begin() as db:
    if model_profiles.default_profile_id(db)==model_profiles.LEGACY_PROFILE_ID:
        p=model_profiles.save_profile(db,{"name":"Synthetic translation bound model","base_url":"https://translation-run.synthetic.test/v1",
            "model_id":"Translation-UI-Bound-Case","api_key":"synthetic-ui-translation-key"})
        model_profiles.set_default(db,p.id)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="127.0.0.1",port=int(sys.argv[1]),log_level="warning")
