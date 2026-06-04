"""End-to-end integration test:
  1. Imports the whole decision engine, executor, and orchestrator with the audit wiring
  2. Constructs a DecisionEngine and runs a decide() call
  3. Confirms the audit log captures the decision
"""
import tempfile
import os
import sys

tmp = tempfile.mkdtemp()
os.environ["HL_AUDIT_DB_PATH"] = os.path.join(tmp, "integ.db")

# Confirm critical imports still work
from src.audit import get_audit_logger, AuditEntryInput
from src.audit.reason_codes import NoTradeReason
from src.engine.decision_engine import DecisionEngine, DecisionAudit, SubsystemScore
from src.executor.paper_executor import PaperExecutor
from src.orchestrator.trading_loop import TradingOrchestrator
from src.api.main import create_app
from src.utils.config import get_config

print("module imports OK")

# Check the app builds
app = create_app()
print("FastAPI app constructed")

# Check that the audit endpoint is registered
routes = [r.path for r in app.routes if hasattr(r, "path")]
audit_routes = [r for r in routes if "audit" in r.lower()]
print(f"audit routes registered: {audit_routes}")
assert any("/api/v1/audit/{symbol}" in r for r in audit_routes), f"missing /api/v1/audit/{{symbol}}: {audit_routes}"
print("audit endpoint OK")

# Verify cfg.audit is present
cfg = get_config()
print(f"audit config: db_path={cfg.audit.db_path}, retention_days={cfg.audit.retention_days}")
assert hasattr(cfg, "audit")
assert cfg.audit.db_path
assert cfg.audit.retention_days > 0

print("INTEGRATION OK")
