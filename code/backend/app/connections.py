"""Paired local owner management, mounted only on the private UI service."""
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .agent_schemas import NewConnection, Pairing
from .agent_server import RequestLimiter, install_errors
from .api_keys import AccessError, KeyStore
from .database import NotFound, new_id


COOKIE = 'mindbox_owner'
COOKIE_PATH = '/api/connections'


def create_owner_app(database):
    keys, limiter = KeyStore(database), RequestLimiter()
    app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
    install_errors(app)
    origins = {f'http://{host}:{port}' for host in ('127.0.0.1', 'localhost', '[::1]') for port in (5173, 5180, 8787)}
    origins.update(o.strip().rstrip('/') for o in os.getenv('MIND_BOX_OWNER_ORIGINS', '').split(',') if o.strip())

    @app.middleware('http')
    async def local_only(request, call_next):
        request.state.request_id = new_id()
        origin = request.headers.get('origin')
        local = ('127.0.0.1', 'localhost', '::1')
        if (request.url.hostname not in local or not request.client or request.client.host not in local or
            origin is not None and origin not in origins or request.headers.get('x-mindbox-request') != '1'):
            return JSONResponse({'error': {'code': 'local_owner_only', 'message': '仅允许本机管理'}}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def owner(request: Request):
        keys.owner(request.cookies.get(COOKIE))

    @app.post('/pair')
    def pair(request: Request, body: Pairing):
        limiter.check('pair:' + request.client.host, 10)
        token = keys.pair(body.code)
        response = JSONResponse({'paired': True})
        response.set_cookie(COOKIE, token, max_age=8 * 3600, httponly=True, samesite='strict', path=COOKIE_PATH,
                            secure=request.url.scheme == 'https')
        return response

    @app.post('/pairing-code')
    def pairing_code(request: Request):
        limiter.check('pairing-code:' + request.client.host, 5)
        return {'code': keys.pairing_code(), 'expires_in_seconds': 600}

    @app.get('/state', dependencies=[Depends(owner)])
    def state():
        with keys.db.connect() as db:
            cabinets = [dict(r) for r in db.execute('SELECT id,name FROM cabinets ORDER BY id')]
        port = int(os.getenv('MIND_BOX_AGENT_PORT', '8788'))
        return {'paired': True, 'connections': keys.list(), 'cabinets': cabinets,
                'api_url': f'http://127.0.0.1:{port}', 'openapi_url': f'http://127.0.0.1:{port}/openapi.json'}

    @app.post('/keys', status_code=201, dependencies=[Depends(owner)])
    def create(body: NewConnection):
        return keys.create(body.name, body.cabinet_ids, body.scopes, body.days)

    @app.post('/keys/{identity}/revoke', dependencies=[Depends(owner)])
    def revoke(identity: str):
        try:
            keys.revoke(identity)
        except NotFound:
            raise AccessError(404, 'not_found', '连接不存在') from None
        return {'revoked': True}

    @app.post('/keys/{identity}/rotate', status_code=201, dependencies=[Depends(owner)])
    def rotate(identity: str):
        try:
            return keys.rotate(identity)
        except NotFound:
            raise AccessError(404, 'not_found', '连接不存在或已失效') from None

    @app.post('/logout', dependencies=[Depends(owner)])
    def logout(request: Request):
        keys.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'paired': False})
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        return response

    return app
