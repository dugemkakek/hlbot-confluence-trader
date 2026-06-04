import tempfile, os
from src.audit import AuditLogger, AuditEntryInput
from src.audit.models import SubsystemScoreRow

tmp = tempfile.mkdtemp()
logger = AuditLogger(db_path=os.path.join(tmp, 'x.db'))
entry = AuditEntryInput(
    symbol='BTC',
    decision='NO_TRADE',
    reason='x',
    subsystem_scores=[SubsystemScoreRow(name='s', raw_score=0.1, adjusted_score=0.1, weight=0.25, is_confirming=True)],
    metadata={'k': 'v'},
)
logger.log(entry)
cur = logger._conn.cursor()
cur.execute("SELECT id, timestamp, symbol, timeframe, decision, reason, reason_code, regime, regime_confidence, final_score, confirming_count, required_confirmations, confluence_score, structure_score, pullback_score, momentum_score, volume_score, confidence, direction, is_actionable, order_id, entry_price, size, stop_loss, take_profit, source, subsystem_scores_json, metadata_json FROM audit_log")
row = cur.fetchone()
print('row type:', type(row).__name__)
print('has keys:', hasattr(row, 'keys'))
if hasattr(row, 'keys'):
    print('keys:', list(row.keys()))
print('idx 25:', row[25])
print('idx 26:', row[26])
print('subsystem_scores_json key:', row['subsystem_scores_json'][:80] if row['subsystem_scores_json'] else None)
print('metadata_json key:', row['metadata_json'])
