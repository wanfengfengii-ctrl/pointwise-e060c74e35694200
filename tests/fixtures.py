import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from account.day import read_day
from catalog.registry import initialize_catalog, open_catalog, register
from intake.rows import SourceRow
from intake.staging import stage
from publication.admission import RetiredPoint, seal
from publication.storage import initialize_warehouse, open_warehouse


DAY = '2026-11-18'


def mixed_rows():
    return (
        SourceRow('device-p', 30, 1, 'L'),
        SourceRow('device-p', 10, 5, 'mL'),
        SourceRow('device-p', 20, None, 'mL', missing=True),
    )


class ReplyLost(Exception):
    """A transport boundary dropped a response after the database committed."""


def drop_reply():
    raise ReplyLost('reply dropped after commit')


@contextmanager
def fail_commit(warehouse, decision):
    """Insert a deferred, unsatisfied FK on an actual formal decision write."""
    table = {'admission': 'fact_batches', 'retirement': 'retirements'}[decision]
    warehouse.execute('PRAGMA temp_store = MEMORY')
    warehouse.execute('CREATE TEMP TABLE commit_guard_parent (id INTEGER PRIMARY KEY)')
    warehouse.execute('''
        CREATE TEMP TABLE commit_guard_child (
            parent_id INTEGER REFERENCES commit_guard_parent(id) DEFERRABLE INITIALLY DEFERRED
        )
    ''')
    warehouse.execute(f'''
        CREATE TEMP TRIGGER trip_commit_guard AFTER INSERT ON main.{table}
        BEGIN INSERT INTO commit_guard_child(parent_id) VALUES (1); END
    ''')
    statements = []
    warehouse.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        warehouse.set_trace_callback(None)
        warehouse.execute('DROP TRIGGER trip_commit_guard')
        warehouse.execute('DROP TABLE commit_guard_child')
        warehouse.execute('DROP TABLE commit_guard_parent')


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='sampling-test-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.catalog_path = root / 'catalog.sqlite3'
        self.warehouse_path = root / 'warehouse.sqlite3'
        self.catalog = None
        self.warehouse = None
        self.addCleanup(self.close_connections)
        self.reopen()
        initialize_catalog(self.catalog)
        initialize_warehouse(self.warehouse)
        register(self.catalog, 'P', 'device-p')

    def close_connections(self):
        if self.catalog is not None:
            self.catalog.close()
            self.catalog = None
        if self.warehouse is not None:
            self.warehouse.close()
            self.warehouse = None

    def reopen(self):
        self.close_connections()
        self.catalog = open_catalog(self.catalog_path)
        self.warehouse = open_warehouse(self.warehouse_path)

    @contextmanager
    def other_connections(self):
        with closing(open_catalog(self.catalog_path)) as catalog:
            with closing(open_warehouse(self.warehouse_path)) as warehouse:
                yield catalog, warehouse

    def take_catalog_offline(self):
        self.catalog.close()
        self.catalog = None
        self.catalog_path.rename(self.catalog_path.with_suffix('.offline'))

    def stage_rows(self, request, rows, point='P', business_day=DAY):
        return stage(self.catalog, self.warehouse, request, point, business_day, rows)

    def stage_value(self, request, value, line=17):
        return self.stage_rows(request, (SourceRow('device-p', line, value, 'mL'),))

    def seal_batch(self, batch, **observers):
        return seal(self.catalog, self.warehouse, batch.request_id, batch.batch_id, **observers)

    def original_receipt(self):
        return self.seal_batch(self.stage_value('original', 5, 4))

    def account(self, point='P', business_day=DAY):
        return read_day(self.catalog, self.warehouse, point, business_day)

    def assert_day(self, count, total, receipts=None):
        account = self.account()
        self.assertEqual((account.row_count, account.total_ml), (count, total))
        if receipts is not None:
            self.assertEqual(account.receipts, receipts)
        return account

    def assert_retired_batch_denied(self, followup, original, **observers):
        rejected = False
        receipt = None
        try:
            receipt = self.seal_batch(followup, **observers)
        except RetiredPoint:
            rejected = True
        account = self.account()
        if (account.row_count, account.total_ml) != (1, 5):
            self.fail(f'RETIRED_POINT_FACT_VISIBLE actual={account.total_ml} expected=5 '
                      f'actual_rows={account.row_count} expected_rows=1')
        self.assertTrue(rejected)
        self.assertIsNone(receipt)
        self.assertEqual(account.receipts, (original,))
