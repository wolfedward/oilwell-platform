"""A test-only venv/spawn/compute topology; never opens a service or database."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time


def tick(path):
    with path.open('ab') as stream:
        stream.write(b'.')


def compute(source, target):
    root=source.parents[3]
    (root/'compute-ready.json').write_text(json.dumps({'pid':os.getpid(),
        'parent_pid':os.getppid(),'base':sys._base_executable}),encoding='utf-8')
    until=time.monotonic()+110
    while time.monotonic()<until:
        tick(root/'compute.ticks');time.sleep(.02)


def supervise(root):
    code=str((root/'code').resolve())
    bootstrap="import sys; from pathlib import Path; sys.path.insert(0,"+repr(code)+"); from backend.inversion.compute_child import compute; compute(Path(sys.argv[1]),Path(sys.argv[2]))"
    stage=root/'private/inversion-staging/inversion-owned'
    child=subprocess.Popen([sys.executable,'-I','-B','-c',bootstrap,str(stage/'input.json'),str(stage/'result.bin')],
        cwd=stage,env=dict(SystemRoot=os.environ['SystemRoot'],TEMP=str(stage),TMP=str(stage),
            PYTHONUTF8='1',PYTHONIOENCODING='utf-8'),creationflags=subprocess.CREATE_NO_WINDOW)
    (root/'supervisor-ready.json').write_text(json.dumps({'pid':os.getpid(),
        'parent_pid':os.getppid(),'launcher_pid':child.pid}),encoding='utf-8')
    while child.poll() is None:
        tick(root/'supervisor.ticks');time.sleep(.02)
    child.wait()


def main():
    root=Path(sys.argv[-1]).parent.parent
    # No descendants until the test attaches this exact Popen handle to its job.
    until=time.monotonic()+30
    while not (root/'contained').exists():
        if time.monotonic()>=until:raise RuntimeError('test containment gate expired')
        time.sleep(.02)
    child=multiprocessing.get_context('spawn').Process(target=supervise,args=(root,))
    child.start()
    (root/'worker-ready.json').write_text(json.dumps({'pid':os.getpid(),
        'parent_pid':os.getppid(),'supervisor_pid':child.pid}),encoding='utf-8')
    child.join(115)
    if child.is_alive():raise RuntimeError('test supervisor did not exit')
    child.close()


if __name__=='__main__':main()
