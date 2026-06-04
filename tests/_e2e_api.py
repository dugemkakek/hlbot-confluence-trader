"""End-to-end test: exercise the /api/v1/audit/{symbol} endpoint via FastAPI TestClient.

This requires FastAPI's TestClient; it's optional in the requirements, so
we skip if it's not available.
"""
import tempfile
import os

tmp = tempfile.mkdtemp()
os.environ["HL_AUDIT_DB_PATH"] = os.path.join(tmp, "e2e.db")

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("SKIP: fastapi.testclient not installed")
    raise SystemExit(0)

from src.audit import get_audit_logger, AuditEntryInput
from src.audit.reason_codes import NoTradeReason
from src.api.main import create_app

# Pre-populate the audit log with some test data
logger = get_audit_logger()
for i in range(3):
    logger.log(
        AuditEntryInput(
            symbol="BTC",
            decision="NO_TRADE",
            reason=f"row {i}",
            reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value,
        )
    )
logger.log(
    AuditEntryInput(
        symbol="ETH",
        decision="NO_TRADE",
        reason="Final score 0.32 below threshold 0.60",
        reason_code=NoTradeReason.FINAL_SCORE_LOW.value,
    )
)
logger.log(
    AuditEntryInput(
        symbol="BTC",
        decision="BUY",
        reason="Trade filled",
        order_id="abc",
        entry_price=50000.0,
        size=0.01,
        stop_loss=49000.0,
        take_profit=52000.0,
    )
)

app = create_app()
client = TestClient(app)

# ── Test 1: /api/v1/audit/BTC returns 3 NO_TRADE + 1 BUY, newest first
r = client.get("/api/v1/audit/BTC?limit=10")
assert r.status_code == 200, r.text
body = r.json()
print(f"BTC: count={body['count']}, limit={body['limit']}")
assert body["count"] == 4, f"expected 4, got {body['count']}"

# Newest first
decisions = [e["decision"] for e in body["entries"]]
print(f"  decisions (newest first): {decisions}")
assert decisions[0] == "BUY", f"expected BUY first, got {decisions[0]}"

# ── Test 2: filter by decision=NO_TRADE
r = client.get("/api/v1/audit/BTC?decision=NO_TRADE&limit=10")
body = r.json()
assert body["count"] == 3
print(f"BTC NO_TRADE: count={body['count']}")

# ── Test 3: filter by reason_code
r = client.get("/api/v1/audit/ETH?reason_code=final_score_low")
body = r.json()
assert body["count"] == 1
assert body["entries"][0]["reason_code"] == "final_score_low"
print(f"ETH by reason_code: count={body['count']}, reason_code={body['entries'][0]['reason_code']}")

# ── Test 4: /reasons endpoint
r = client.get("/api/v1/audit/BTC/reasons")
body = r.json()
print(f"BTC reasons: {body}")
assert body["counts"]["insufficient_confirmations"] == 3
assert body["total"] == 3

# ── Test 5: a trade row has all the trade-specific fields
buy_entry = next(e for e in client.get("/api/v1/audit/BTC?decision=BUY").json()["entries"])
print(f"BUY entry: order_id={buy_entry['order_id']} entry={buy_entry['entry_price']} sl={buy_entry['stop_loss']} tp={buy_entry['take_profit']}")
assert buy_entry["order_id"] == "abc"
assert buy_entry["entry_price"] == 50000.0
assert buy_entry["size"] == 0.01
assert buy_entry["stop_loss"] == 49000.0
assert buy_entry["take_profit"] == 52000.0

print("E2E API TESTS PASSED")
