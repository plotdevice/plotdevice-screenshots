# loaded by the app's (or the command line tool's) embedded python via PYTHONPATH; only drives the gui when asked to
import os
if os.environ.get('PD_SHOTS_DIR'):
    import shots
    if os.environ.get('PD_SHOTS_MODE') == 'viewer':
        shots.start_viewer()
    else:
        shots.start()
