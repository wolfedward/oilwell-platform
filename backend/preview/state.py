"""Only preview-created state lives here; business snapshots are never mutated."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class PreviewState:
    def __init__(self,path: Path):
        self.path=Path(path).resolve()
        with self.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS rehearsal_events(
              sequence INTEGER PRIMARY KEY, request_id TEXT NOT NULL, actor_id TEXT NOT NULL,
              object_id TEXT NOT NULL, action TEXT NOT NULL, version INTEGER NOT NULL,
              fingerprint TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL,
              UNIQUE(actor_id,request_id), UNIQUE(object_id,version));''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            yield db;db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def history(self,object_id=None):
        with self.connection() as db:
            sql='SELECT result FROM rehearsal_events';args=()
            if object_id is not None:sql+=' WHERE object_id=?';args=(object_id,)
            return [json.loads(row[0]) for row in db.execute(sql+' ORDER BY sequence',args)]

    def current(self,object_id):
        records=self.history(object_id)
        return records[-1] if records else {'object_id':object_id,'version':0,'values':{},'mode':'rehearsal'}

    def reset(self,confirmation):
        from .errors import PreviewError
        if confirmation!='RESET_REHEARSALS': raise PreviewError('confirmation_required','请明确确认重置演练')
        with self.connection() as db: db.execute('DELETE FROM rehearsal_events')
