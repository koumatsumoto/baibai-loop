"""企業成長の公開読取入口。対象を候補所属や現在価値の充足で制限しない。"""

from baibai_engine.company_growth.models import CompanyGrowth
from baibai_engine.company_growth.query import company_growth

__all__ = ["CompanyGrowth", "company_growth"]
