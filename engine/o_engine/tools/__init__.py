"""Engine-side system tools. Each module exposes plain functions plus a FastAPI router that ``server.py``
includes only on the platform the module serves (``win_tools`` on ``sys.platform == 'win32'``)."""
