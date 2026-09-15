"""Owned desktop backend: private data cwd, loopback socket, parent-pipe shutdown."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--check-browser', action='store_true')
    args = parser.parse_args()
    data = Path(args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    assets = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    os.chdir(data)
    os.environ['DOLA_ENV_FILE'] = str(data / '.env.local')
    os.environ['DOLA_WEB_DIR'] = str(assets / 'web')
    os.environ.setdefault('DOLA_EXTENSION_DIR', str(assets / 'extensions' / 'dola30'))
    # Desktop owns its databases; never inherit paths into a running CLI project.
    os.environ['DOLA_DB_PATH'] = str(data / 'tasks.db')
    os.environ['DOLA_DOWNLOAD_DIR'] = str(data / 'downloads')
    if args.check_browser:
        async def check_browser():
            from patchright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('<title>Dola packaged browser check</title>')
                    assert await page.title() == 'Dola packaged browser check'
                    print('PACKAGED_BROWSER_OK', flush=True)
                finally:
                    await browser.close()
        asyncio.run(check_browser())
        return
    sock = socket.socket()
    port_file = data / '.desktop-port'
    saved_port = int(port_file.read_text().strip()) if port_file.exists() else 0
    # Keep the dashboard origin stable so localStorage survives restarts.
    # Never attach to (or terminate) an unrelated listener on this port.
    try:
        sock.bind(('127.0.0.1', saved_port))
    except OSError as exc:
        raise RuntimeError(f'Desktop port {saved_port} is occupied; close the other Dola desktop instance.') from exc
    port = sock.getsockname()[1]
    port_file.write_text(str(port), encoding='utf-8')
    os.environ['DOLA_PORT'] = str(port)
    os.environ['DOLA_PUBLIC_BASE'] = f'http://127.0.0.1:{port}'
    import uvicorn
    from server import app
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         loop='asyncio', timeout_graceful_shutdown=20))

    def control():
        # EOF also handles an Electron crash; no orphan listening server.
        sys.stdin.readline()
        server.should_exit = True

    threading.Thread(target=control, daemon=True).start()

    async def run():
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not task.done():
            await asyncio.sleep(.05)
        if server.started:
            print('DOLA_DESKTOP_READY ' + json.dumps({'port': port}), flush=True)
        await task

    try:
        asyncio.run(run())
    finally:
        sock.close()


if __name__ == '__main__':
    main()
