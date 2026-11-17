"""Isolated real-HTTP desktop handshake fixture; no provider configuration."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files=lambda self:{}

from app.main import app
from app.local_session import instance_session
from fastapi import WebSocket
from fastapi.responses import Response
import uvicorn

port=int(sys.argv[1])
secret=instance_session.enable(f'http://127.0.0.1:{port}')
# Anonymous stdout pipe, consumed in memory by the parent. Never put the
# handoff in environment variables, test artifacts, access logs or arguments.
print('LAUNCH:'+secret, flush=True)

@app.websocket('/api/local-fixture/socket')
async def echo(ws: WebSocket):
    await ws.accept()
    await ws.send_text('authenticated')
    await ws.close()

@app.get('/api/local-fixture/export')
def export():
    return Response('Synthetic authorized download',media_type='text/plain',headers={
        'Content-Disposition':'attachment; filename="instance-fixture.txt"'})

@app.post('/api/local-fixture/seed-pdf')
def seed_pdf():
    from app.db import SessionLocal
    from app.config import DATA_DIR
    from app.models import Paper
    from reportlab.pdfgen import canvas
    file=DATA_DIR/'instance-fixture.pdf'
    pdf=canvas.Canvas(str(file));pdf.drawString(40,760,'Synthetic authorized PDF');pdf.save()
    with SessionLocal() as db:
        if db.get(Paper,'local-auth-pdf') is None:
            db.add(Paper(id='local-auth-pdf',title='Synthetic authorized PDF',pdf_path=str(file),status='ready',page_count=1))
            db.commit()
    return {'paper_id':'local-auth-pdf'}

if __name__=='__main__':
    # The production SPA mount is last among production routes. Keep fixture
    # routes before it rather than letting StaticFiles claim their requests.
    app.router.routes.sort(key=lambda route: 0 if getattr(route,'path','').startswith('/api/local-fixture/') else 1)
    try:
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning',access_log=False)
    finally:
        instance_session.close()
