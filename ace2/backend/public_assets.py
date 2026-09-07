"""Explicit public files; backend/configuration can never fall through to static."""
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
PUBLIC_FILES = frozenset({'index.html', 'app.js', 'styles.css', 'sw.js', 'manifest.json',
    'icon-180.png', 'icon-192.png', 'icon-512.png', 'icon-maskable.png', 'review.js', 'review.css'})
class PublicAssets(StaticFiles):
    async def get_response(self, path, scope):
        if path not in PUBLIC_FILES:
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)
