import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# scripts/_clean_lib_test_docs.py
from gfvfw.db import SessionLocal
from gfvfw.models import Document
from gfvfw.services import library

with SessionLocal() as s:
    for d in s.query(Document).all():
        p = library.storage_path_for(d)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
        print("删除文档", d.id, d.original_filename)
        s.delete(d)
    s.commit()