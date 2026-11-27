import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date
from app.db import connect, init_schema
from app import seed, services as S


class DBCase(unittest.TestCase):
    """Fresh database per test: masters + opening stock (benchmark NOT yet run)."""
    benchmark = False

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = connect(self.path)
        init_schema(self.conn)
        seed.load_masters(self.conn, date.today())
        self.ids = seed.run_benchmark(self.conn) if self.benchmark else {}

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def mat(self, code):
        return self.conn.execute("SELECT id FROM materials WHERE code=?", (code,)).fetchone()[0]

    def bal(self, code):
        return S.stock_balance(self.conn, material_id=self.mat(code))

    def fg(self):
        pid = self.conn.execute("SELECT id FROM products WHERE sku='MUR200'").fetchone()[0]
        return S.stock_balance(self.conn, product_id=pid)
