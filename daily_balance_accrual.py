from __future__ import annotations

import json

from ferias_app.services.postgres_service import init_db
from ferias_app.services.period_accrual_service import business_today, ensure_due_periods

if __name__ == "__main__":
    init_db(run_migrations=False)
    print(json.dumps(ensure_due_periods(business_today()), ensure_ascii=False, default=str))
