"""
ledger/registry/client.py — Applicant Registry read-only client
===============================================================
COMPLETION STATUS: STUB — implement the query methods.

This client reads from the applicant_registry schema in PostgreSQL.
It is READ-ONLY. No agent or event store component ever writes here.
The Applicant Registry is the external CRM — seeded by datagen/generate_all.py.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Any

@dataclass
class CompanyProfile:
    company_id: str; name: str; industry: str; naics: str
    jurisdiction: str; legal_type: str; founded_year: int
    employee_count: int; risk_segment: str; trajectory: str
    submission_channel: str; ip_region: str

@dataclass
class FinancialYear:
    fiscal_year: int; total_revenue: float; gross_profit: float
    operating_income: float; ebitda: float; net_income: float
    total_assets: float; total_liabilities: float; total_equity: float
    long_term_debt: float; cash_and_equivalents: float
    current_assets: float; current_liabilities: float
    accounts_receivable: float; inventory: float
    debt_to_equity: float; current_ratio: float
    debt_to_ebitda: float; interest_coverage_ratio: float
    gross_margin: float; ebitda_margin: float; net_margin: float

@dataclass
class ComplianceFlag:
    flag_type: str; severity: str; is_active: bool; added_date: str; note: str

class ApplicantRegistryClient:
    """
    READ-ONLY access to the Applicant Registry.
    Agents call these methods to get company profiles and historical data.
    Never write to this database from the event store system.
    """

    def __init__(self, pool: Any):
        """pool: asyncpg.Pool when using PostgreSQL."""
        self._pool = pool

    async def get_company(self, company_id: str) -> CompanyProfile | None:
        """SELECT * FROM applicant_registry.companies WHERE company_id = $1"""
        if self._pool is None:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT company_id, name, industry, naics, jurisdiction, legal_type,
                       founded_year, employee_count, risk_segment, trajectory,
                       submission_channel, ip_region
                FROM applicant_registry.companies
                WHERE company_id = $1
                """,
                company_id,
            )
        if not row:
            return None
        return CompanyProfile(
            company_id=str(row["company_id"]),
            name=str(row["name"]),
            industry=str(row["industry"]),
            naics=str(row["naics"]),
            jurisdiction=str(row["jurisdiction"]),
            legal_type=str(row["legal_type"]),
            founded_year=int(row["founded_year"]),
            employee_count=int(row["employee_count"]),
            risk_segment=str(row["risk_segment"]),
            trajectory=str(row["trajectory"]),
            submission_channel=str(row["submission_channel"]),
            ip_region=str(row["ip_region"]),
        )

    async def get_financial_history(self, company_id: str,
                                     years: list[int] | None = None) -> list[FinancialYear]:
        """
        SELECT * FROM applicant_registry.financial_history
        WHERE company_id = $1 [AND fiscal_year = ANY($2)]
        ORDER BY fiscal_year ASC
        """
        if self._pool is None:
            return []
        query = (
            "SELECT fiscal_year, total_revenue, gross_profit, operating_income, ebitda, "
            "net_income, total_assets, total_liabilities, total_equity, long_term_debt, "
            "cash_and_equivalents, current_assets, current_liabilities, accounts_receivable, "
            "inventory, debt_to_equity, current_ratio, debt_to_ebitda, interest_coverage_ratio, "
            "gross_margin, ebitda_margin, net_margin "
            "FROM applicant_registry.financial_history "
            "WHERE company_id = $1"
        )
        params: list[Any] = [company_id]
        if years:
            query += " AND fiscal_year = ANY($2)"
            params.append(years)
        query += " ORDER BY fiscal_year ASC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        out: list[FinancialYear] = []
        for row in rows:
            out.append(
                FinancialYear(
                    fiscal_year=int(row["fiscal_year"]),
                    total_revenue=float(row["total_revenue"]),
                    gross_profit=float(row["gross_profit"]),
                    operating_income=float(row["operating_income"]),
                    ebitda=float(row["ebitda"]),
                    net_income=float(row["net_income"]),
                    total_assets=float(row["total_assets"]),
                    total_liabilities=float(row["total_liabilities"]),
                    total_equity=float(row["total_equity"]),
                    long_term_debt=float(row["long_term_debt"]),
                    cash_and_equivalents=float(row["cash_and_equivalents"]),
                    current_assets=float(row["current_assets"]),
                    current_liabilities=float(row["current_liabilities"]),
                    accounts_receivable=float(row["accounts_receivable"]),
                    inventory=float(row["inventory"]),
                    debt_to_equity=float(row["debt_to_equity"] or 0.0),
                    current_ratio=float(row["current_ratio"] or 0.0),
                    debt_to_ebitda=float(row["debt_to_ebitda"] or 0.0),
                    interest_coverage_ratio=float(row["interest_coverage_ratio"] or 0.0),
                    gross_margin=float(row["gross_margin"] or 0.0),
                    ebitda_margin=float(row["ebitda_margin"] or 0.0),
                    net_margin=float(row["net_margin"] or 0.0),
                )
            )
        return out

    async def get_compliance_flags(self, company_id: str,
                                    active_only: bool = False) -> list[ComplianceFlag]:
        """
        SELECT * FROM applicant_registry.compliance_flags
        WHERE company_id = $1 [AND is_active = TRUE]
        """
        if self._pool is None:
            return []
        query = (
            "SELECT flag_type, severity, is_active, added_date, note "
            "FROM applicant_registry.compliance_flags "
            "WHERE company_id = $1"
        )
        if active_only:
            query += " AND is_active = TRUE"
        query += " ORDER BY added_date DESC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, company_id)
        return [
            ComplianceFlag(
                flag_type=str(row["flag_type"]),
                severity=str(row["severity"]),
                is_active=bool(row["is_active"]),
                added_date=self._date_to_iso(row["added_date"]),
                note=str(row["note"] or ""),
            )
            for row in rows
        ]

    async def get_loan_relationships(self, company_id: str) -> list[dict]:
        """SELECT * FROM applicant_registry.loan_relationships WHERE company_id = $1"""
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT loan_amount, loan_year, was_repaid, default_occurred, note
                FROM applicant_registry.loan_relationships
                WHERE company_id = $1
                ORDER BY loan_year ASC
                """,
                company_id,
            )
        return [
            {
                "loan_amount": float(row["loan_amount"]),
                "loan_year": int(row["loan_year"]),
                "was_repaid": bool(row["was_repaid"]),
                "default_occurred": bool(row["default_occurred"]),
                "note": str(row["note"] or ""),
            }
            for row in rows
        ]

    @staticmethod
    def _date_to_iso(v: Any) -> str:
        if isinstance(v, date):
            return v.isoformat()
        return str(v or "")
